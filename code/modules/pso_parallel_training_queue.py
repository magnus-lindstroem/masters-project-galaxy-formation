import numpy as np
import time
import datetime
import multiprocessing as mp
import queue
import pickle
# from keras.models import Sequential
# from keras.layers import Dense
from model_setup import *
from data_processing import get_weights, predict_points, loss_func_obs_stats
from sklearn.metrics import mean_squared_error

class Feed_Forward_Neural_Network():
    
    def __init__(self, nr_hidden_layers, nr_neurons_per_lay, input_features, output_features, 
                 activation_function, output_activation, reg_strength, network_name):
        
        self.nr_hidden_layers = nr_hidden_layers
        self.nr_neurons_per_lay = nr_neurons_per_lay
        self.input_features = input_features
        self.output_features = output_features
        self.activation_function = activation_function
        self.output_activation = output_activation
        self.reg_strength = reg_strength
        self.name = network_name
                
    def setup_pso(self, pso_param_dict={}, reinf_learning=True, real_observations=True, nr_processes=30):
        
        self.pso_swarm = PSO_Swarm(self, self.nr_hidden_layers, self.nr_neurons_per_lay, self.input_features, 
                                   self.output_features, self.activation_function, self.output_activation,
                                   pso_param_dict, self.reg_strength, reinf_learning, real_observations, nr_processes)
        
    def train_pso(self, nr_iterations, training_data_dict, speed_check=False, std_penalty=False, verbatim=False):
        
        self.pso_swarm.train_network(nr_iterations, training_data_dict,
                                     std_penalty, speed_check, verbatim)
        

class PSO_Swarm(Feed_Forward_Neural_Network):
    
    def __init__(self, parent, nr_hidden_layers, nr_neurons_per_lay, input_features, output_features, 
                 activation_function, output_activation, pso_param_dict, reg_strength, reinf_learning, real_observations,
                 nr_processes):
        self.pso_param_dict = {
            'nr_particles': 40,
            'xMin': -10,
            'xMax': 10,
            'alpha': 1,
            'deltaT': 1,
            'c1': 2,
            'c2': 2,
            'inertiaWeightStart': 1.4,
            'inertiaWeightMin': 0.3,
            'explorationFraction': 0.8,
            'min_std_tol': 0.01,
            'patience': 300,
            'patience_parameter': 'val',
            'restart_check_interval': 100
        }
    
        if pso_param_dict is not None:
            for key in pso_param_dict:
                if key in self.pso_param_dict:
                    self.pso_param_dict[key] = pso_param_dict[key]
                else:
                    print('\'%s\ is not a valid key. Choose between:' % (key), self.pso_param_dict.keys())
                    break
                    
        self.parent = parent
        self.reinf_learning = reinf_learning
        self.train_on_real_obs = real_observations
        self.nr_processes = nr_processes
        self.nr_hidden_layers = nr_hidden_layers
        self.nr_neurons_per_lay = nr_neurons_per_lay
        self.activation_function = activation_function
        self.input_features = input_features
        self.output_features = output_features
                
        self.nr_variables, self.weight_shapes = standard_network_get_nr_variables_weight_shapes(input_features, output_features, 
                                                                                                nr_neurons_per_lay, nr_hidden_layers)
        
        self.network_args = [input_features, output_features, nr_neurons_per_lay, nr_hidden_layers, 
                             activation_function, output_activation, reg_strength]
        
        self.best_weights_train = None
        self.best_weights_val = None
        
        self.inertia_weight = self.pso_param_dict['inertiaWeightStart']
        self.vMax = (self.pso_param_dict['xMax'] - self.pso_param_dict['xMin']) / self.pso_param_dict['deltaT']
                
    def train_network(self, nr_iterations, training_data_dict, std_penalty, speed_check, verbatim):
        
        self.training_data_dict = training_data_dict
        
        self.nr_iterations_trained = nr_iterations
        self.std_penalty = std_penalty
        self.verbatim = verbatim
        
        with open('progress.txt', 'w+') as f:
            
            self.inp_queue = mp.Queue()
            self.results_queue = mp.Queue()
            
            process_list = []
            for i in range(self.nr_processes):
                process = mp.Process(target=particle_evaluator, args=(self.inp_queue, self.results_queue, training_data_dict, 
                                                                      self.reinf_learning, self.train_on_real_obs, 
                                                                      self.network_args, self.weight_shapes))
                process_list.append(process)
                
            for process in process_list:
                process.start()
            
            should_start_fresh = True
            while should_start_fresh:
                should_start_fresh = False

                inertia_weight_reduction = np.exp(np.log(self.pso_param_dict['inertiaWeightMin'] 
                                                         / self.pso_param_dict['inertiaWeightStart'])
                                                         / self.pso_param_dict['explorationFraction'] 
                                                         / nr_iterations)
                inertia_weight = self.pso_param_dict['inertiaWeightStart']
                
                self.progress = 0
                
                self.swarm_best_score_train = 1e20
                self.swarm_best_score_val = 1e20
                
                self.validation_score_history = []
                self.training_score_history = []
                
                self.avg_speed_before_history = []
                self.avg_speed_after_history = []
                    
                self.time_since_train_improvement = 0
                self.time_since_val_improvement = 0
                
                self.initialise_positions_velocities()

                glob_start = time.time()
                for iteration in range(nr_iterations):
                    
                    self.time_since_train_improvement += 1
                    self.time_since_val_improvement += 1
                    self.progress = iteration / nr_iterations

                    should_start_fresh, training_is_done, end_train_message = self.check_progress(f, glob_start, iteration)
                    if should_start_fresh or training_is_done:
                        break

                    for i_particle in range(self.pso_param_dict['nr_particles']):
                        self.inp_queue.put([self.positions[i_particle], i_particle, 'train'])

                    particle_scores = np.zeros(self.pso_param_dict['nr_particles'])
                    for i_particle in range(self.pso_param_dict['nr_particles']):
                        
                        try:
                            score, particle_nr = self.results_queue.get(timeout=100)
                            
                        except queue.Empty:
                            print('timeout error')
                            process_list[i_particle].terminate()
                            
                        particle_scores[particle_nr] = score
                    
                    # results contain the train scores. if one of them exceeds the max: update the max score, best swarm 
                    # position and check the val score of the best pos
                    self.update_loss_stats(particle_scores, f, iteration)


                    # update the positions of the particles, the function should receive the swarm best, the particle best 
                    # and the velocities

                    # 

#                     self.update_best_weights(particle, train_score, f, iteration, i_particle)

                    self.update_swarm(speed_check, f)

                    inertia_weight = self.update_inertia_weight(inertia_weight, inertia_weight_reduction, 
                                                                iteration, f)
                    
            end = time.time()
            if end_train_message is None:
                end_train_message = 'Training ran through all {:d} iterations without premature stopping.'.format(nr_iterations)
                
            if self.verbatim:
                print('{}, Training complete. {}'.format(datetime.datetime.now().strftime("%H:%M:%S"), end_train_message))
            f.write('{}, Training complete. {}'.format(datetime.datetime.now().strftime("%H:%M:%S"), end_train_message))
            f.flush()
            
    def update_loss_stats(self, results, f, iteration):
    
        for i_particle, result in enumerate(results):
            
            is_particle_best = result < self.particle_train_best_scores[i_particle]
            
            if is_particle_best:
                self.particle_train_best_scores[i_particle] = result
                self.particle_train_best_pos[i_particle] = self.positions[i_particle]
                
            is_swarm_best_train = result < self.swarm_best_score_train
            
            if is_swarm_best_train:
                
                self.swarm_best_score_train = result
                self.training_score_history.append(result)
                self.swarm_best_pos_train = self.positions[i_particle]
                
                self.time_since_train_improvement = 0
                
                # see if the result is also the highest val result so far
                self.inp_queue.put([self.positions[i_particle], i_particle, 'val'])
                val_score, stds = self.results_queue.get(timeout=100)
                self.best_val_stds = stds
                
                is_swarm_best_val = val_score < self.swarm_best_score_val
                
                if is_swarm_best_val:
                    
                    self.swarm_best_pos_val = self.positions[i_particle]
                    self.validation_score_history.append(val_score)
                    self.swarm_best_score_val = val_score
                    
                    self.time_since_val_improvement = 0
                    
                    # save the model
                    self.inp_queue.put([self.positions[i_particle], i_particle, 'save {}'.format(self.parent.name)])
                    out = self.results_queue.get(timeout=20)
                    
                    if out != 'save_successful':
                        print('out: ', out)
                        print('model could not be saved')
                    
                if self.verbatim:
                    print('{}  Iteration {:4d}, particle {:2d}, new swarm best. Train: {:.3e}, Val: {:.3e}'.format(
                          datetime.datetime.now().strftime("%H:%M:%S"), iteration, i_particle, result, val_score))
                f.write('{}  Iteration {:4d}, particle {:2d}, new swarm best. Train: {:.3e}, Val: {:.3e}\n'.format(
                      datetime.datetime.now().strftime("%H:%M:%S"), iteration, i_particle, result, val_score))
                f.flush()
                    
            
#     def update_best_weights(self, results, f, iteration):
        
#         self.best_weights_train = particle.get_weights()
                        
#         self.time_since_train_improvement = 0
#         self.swarm_best_train = train_score
#         self.swarm_best_position = particle.position

#         val_score = particle.evaluate_particle('val')
#         is_swarm_best_val = (val_score < self.swarm_best_val)
#         if is_swarm_best_val: # only update best weights after val highscore
            
#             self.best_weights_val = particle.get_weights()
#             self.swarm_best_val = val_score
#             self.time_since_val_improvement = 0

#         self.validation_score_history.append(val_score)
#         self.training_score_history.append(train_score)
        
#         if self.verbatim:
#             print('{}  Iteration {:4d}, particle {:2d}, new swarm best. Train: {:.3e}, Val: {:.3e}'.format(
#                   datetime.datetime.now().strftime("%H:%M:%S"), iteration, i_particle, train_score, val_score))
#         f.write('{}  Iteration {:4d}, particle {:2d}, new swarm best. Train: {:.3e}, Val: {:.3e}\n'.format(
#               datetime.datetime.now().strftime("%H:%M:%S"), iteration, i_particle, train_score, val_score))
#         f.flush()
    
    
    def check_progress(self, f, glob_start, iteration):
        
        should_start_fresh = False
        training_is_done = False
        end_train_message = None
        
        if self.pso_param_dict['patience_parameter'] == 'val':
            if self.time_since_val_improvement > self.pso_param_dict['patience']:
                training_is_done = True
                end_train_message = 'Early stopping in iteration {}. Max patience for val loss improvement reached ({})'.format(
                                     iteration, self.pso_param_dict['patience'])
        elif self.pso_param_dict['patience_parameter'] == 'train':
            if self.time_since_train_improvement > self.pso_param_dict['patience']:
                training_is_done = True
                end_train_message = 'Early stopping in iteration {}. Max patience for train loss improvement reached ({})'.format(
                                     iteration, self.pso_param_dict['patience'])
        
        if (int(iteration/self.pso_param_dict['restart_check_interval']) == iteration/self.pso_param_dict['restart_check_interval']) \
            and (iteration > 0):
            
#             print('\nStandard deviations of predicted parameters (validation set): ', stds)
            should_start_fresh = np.any(self.best_val_stds < self.pso_param_dict['min_std_tol'])
            if should_start_fresh:
                if self.verbatim:
                    print('Restarting training because of too low predicted feature variance in validation set ({:.2e}).\n'.format(
                          np.min(self.best_val_stds)))
                f.write('Restarting training because of too low predicted feature variance in validation set ({:.2e}).\n'.format(
                      np.min(self.best_val_stds)))
                return [should_start_fresh, training_is_done, end_train_message]

            progress_end = time.time()
            elapsed_so_far = int((progress_end - glob_start) / 60)
            time_remaining = int(elapsed_so_far / iteration * (self.nr_iterations_trained - iteration))

            if self.verbatim:
                print('\n{}, Iteration {:d}\n'.format(datetime.datetime.now().strftime("%H:%M:%S"), iteration))
            f.write('\n{}      '.format(datetime.datetime.now().strftime("%H:%M:%S")))
            f.write('Iterations tried: {:d}/{:d}     '.format(iteration, self.nr_iterations_trained))
            f.write('Elapsed time: {:d}min     '.format(elapsed_so_far))
            f.write('Time remaining: {:d}min.\n'.format(time_remaining))
            f.flush()
            
        return [should_start_fresh, training_is_done, end_train_message]
        
        
    def update_inertia_weight(self, inertia_weight, inertia_weight_reduction, iteration, f):
        
        isExploring = (inertia_weight > self.pso_param_dict['inertiaWeightMin'])
        if isExploring:
            inertia_weight = inertia_weight * inertia_weight_reduction
            isExploring = (inertia_weight > self.pso_param_dict['inertiaWeightMin'])
            if not isExploring:
                if self.verbatim:
                    print('SWITCH TO EPLOIT! Iteration %d/%d.' % (iteration, self.nr_iterations_trained))
                f.write('SWITCH TO EPLOIT! Iteration %d/%d.\n' % (iteration, self.nr_iterations_trained))
                f.flush()
        return inertia_weight
        
#     def predict_output_old(self, mode):
        
#         if self.training_data_dict['norm'] == 'none':
#             y_pred = self.parent.model.predict(self.training_data_dict['x_'+mode])
#         else:
#             y_pred = self.parent.model.predict(self.training_data_dict['x_'+mode+'_norm'])

#         return y_pred
        
    def update_swarm(self, speed_check, f):
        
#         self.speeds_before = []
#         self.speeds_after = []
        
        for i_particle in range(self.pso_param_dict['nr_particles']):
            self.update_particle(i_particle)
        
#         avg_speed_before = np.mean(self.speeds_before)
#         avg_speed_after = np.mean(self.speeds_after)
#         self.avg_speed_before_history.append(avg_speed_before)
#         self.avg_speed_after_history.append(avg_speed_after)
        
        if speed_check:
            print('Average speed of the particles before normalization is: ', avg_speed_before)
            print('Average speed of the particles after normalization is: ', avg_speed_after)
            f.write('Average speed of the particles before normalization is: %.2f' % (avg_speed_before))
            f.write('Average speed of the particles after normalization is: %.2f' % (avg_speed_after))
            f.flush()
            
#     def set_best_weights(self, mode):
#         if mode == 'train':
#             self.parent.model.set_weights(self.best_weights_train)
#         elif mode == 'val':
#             self.parent.model.set_weights(self.best_weights_val)
        
    def evaluate_model(self, model, training_data_dict, mode):
            
        y_pred = predict_points(model, training_data_dict, original_units=False, as_lists=False, mode=mode)
        
        if self.reinf_learning:
            
            score = loss_func_obs_stats(self.parent.model, self.training_data_dict, real_obs=self.train_on_real_obs, mode=mode)
            
        else:
            
       #     diff = y_pred - self.training_data_dict['y_'+mode]
       #     squares = np.power(diff, 2) / np.shape(y_pred[0])
       #     weighted_squares = squares * self.training_data_dict[mode+'_weights'] / np.sum(self.training_data_dict[mode+'_weights'])
       #     score = np.sum(self.training_data_dict[mode+'_weights'])

            score = mean_squared_error(self.training_data_dict['y_'+mode], y_pred, self.training_data_dict[mode+'_weights'])
       #     print('score diff: {:.2e}'.format(score2 - score))
    
            if weights is None:
                weights = np.array([])
                weight_mat_list = model.get_weights()
                for weight_mat in weight_mat_list:
                    weights = np.concatenate((weights, np.ndarray.flatten(weight_mat)))

            score = score + np.sum(np.power(weights, 2) * self.reg_strength)
            
        return score
    
    
    def initialise_positions_velocities(self):
                    
        self.positions = []
        self.velocities = []
        self.particle_train_best_scores = []
        self.particle_train_best_pos = []
        
        for i_particle in range(self.pso_param_dict['nr_particles']):
            r1 = np.random.uniform(size=(self.nr_variables))
            r2 = np.random.uniform(size=(self.nr_variables))

            position = self.pso_param_dict['xMin'] + r1 * (self.pso_param_dict['xMax'] - 
                                    self.pso_param_dict['xMin'])
            velocity = self.pso_param_dict['alpha']/self.pso_param_dict['deltaT'] * \
                            ((self.pso_param_dict['xMin'] - self.pso_param_dict['xMax'])/2 + r2 * 
                             (self.pso_param_dict['xMax'] - self.pso_param_dict['xMin']))

            starting_score = 1e20
            
            self.positions.append(position)
            self.velocities.append(velocity)
            self.particle_train_best_scores.append(starting_score)
            self.particle_train_best_pos.append(position)
            
        self.swarm_best_pos_train = self.positions[0]
        self.swarm_best_pos_val = self.positions[0]
        
    def evaluate_particle(self, model, position, training_data_dict, mode):
        
        weight_mat_list = self.get_weights(position)
        model.set_weights(weight_mat_list)
        
        score = self.evaluate_model(model, training_data_dict, mode)
            
        return score
            
    def get_weights(self, position): # get the weight list corresponding to the position in parameter space
        
        weight_mat_list = [] # will contain the weight matrices 
        weight_counter = 0 # to help assign weights and biases to their correct matrix
        
        for mat_shape in self.weight_shapes:
            nr_weights = np.prod(mat_shape)
            weights = position[weight_counter : weight_counter+nr_weights]
            mat = np.reshape(weights, mat_shape)
            weight_mat_list.append(mat)
            weight_counter += nr_weights
        
        return weight_mat_list
        

    def update_particle(self, i_particle):

        q = np.random.uniform()
        r = np.random.uniform()
        
        particle_best_difference = self.particle_train_best_pos[i_particle] - self.positions[i_particle]
        swarm_best_difference = self.swarm_best_pos_train - self.positions[i_particle]

        self.velocities[i_particle] = self.inertia_weight * self.velocities[i_particle] + self.pso_param_dict['c1'] * q * \
                                      particle_best_difference / self.pso_param_dict['deltaT'] + \
                                      self.pso_param_dict['c2'] * r * swarm_best_difference / \
                                      self.pso_param_dict['deltaT']
        
        # now limit velocity to vMax
        absolute_velocity_before_normalization = np.sqrt(np.sum(np.power(self.velocities[i_particle], 2)))
        is_too_fast = absolute_velocity_before_normalization > self.vMax
        if is_too_fast:
            #self.parent.too_fast_count += 1
            self.velocities[i_particle] = self.velocities[i_particle] * self.vMax / absolute_velocity_before_normalization
            
        absolute_velocity_after_normalization = np.sqrt(np.sum(np.power(self.velocities[i_particle], 2)))

#         self.parent.speeds_before.append(absolute_velocity_before_normalization)
#         self.parent.speeds_after.append(absolute_velocity_after_normalization)
            
        self.positions[i_particle] = self.positions[i_particle] + self.velocities[i_particle] * self.pso_param_dict['deltaT']
        
    def get_best_weights(self, mode):
        
        if mode == 'train':
            weights = self.get_weights(self.swarm_best_pos_train)
        elif mode == 'val':
            weights = self.get_weights(self.swarm_best_pos_val)
            
        return weights
        
def particle_evaluator(inp_queue, results_queue, training_data_dict, reinf_learning, train_on_real_obs, network_args, 
                       weight_shapes):
    
#     import tensorflow as tf
#     sess = tf.Session()
#     from keras import backend as K
#     K.set_session(sess)

    input_features, output_features, nr_neurons_per_lay, nr_hidden_layers, \
                    activation_function, output_activation, reg_strength = network_args
    model = standard_network(input_features, output_features, nr_neurons_per_lay, nr_hidden_layers, 
                             activation_function, output_activation, reg_strength)
    
    keep_evaluating = True
    while keep_evaluating:
        position, particle_nr, string = inp_queue.get()
        
        if position is None:
            keep_evaluating = False
            print('particle evaluator stopped')
        else:

            weight_mat_list = get_weights(position, weight_shapes)
            model.set_weights(weight_mat_list)

            str_list = string.split(' ')
            
            if len(str_list) == 1:
                mode = str_list[0]
            elif len(str_list) == 2:
                mode = str_list[0]
                name = str_list[1]
                
            if mode == 'train':
                score = evaluate_model(model, training_data_dict, reinf_learning, train_on_real_obs, mode=mode)
                results_queue.put([score, particle_nr])
            elif mode == 'val':
                
                score = evaluate_model(model, training_data_dict, reinf_learning, train_on_real_obs, mode=mode)
                y_pred = predict_points(model, training_data_dict, original_units=False, mode=mode)

                stds = np.std(y_pred, axis=0)
                
                results_queue.put([score, stds])
                
            elif mode == 'save':
                
                model.save('trained_networks/{}.h5'.format(name))
                pickle.dump(training_data_dict, open('trained_networks/{}_training_data_dict.p'.format(name), 'wb'))
#                 np.save('trained_networks/best_model_training_data_dict.npy', training_data_dict)
                
                results_queue.put('save_successful')

def get_weights(position, weight_shapes): # get the weight list corresponding to the position in parameter space

    weight_mat_list = [] # will contain the weight matrices 
    weight_counter = 0 # to help assign weights and biases to their correct matrix

    for mat_shape in weight_shapes:
        nr_weights = np.prod(mat_shape)
        weights = position[weight_counter : weight_counter+nr_weights]
        mat = np.reshape(weights, mat_shape)
        weight_mat_list.append(mat)
        weight_counter += nr_weights
        
    return weight_mat_list

def evaluate_model(model, training_data_dict, reinf_learning, train_on_real_obs, mode):
    
    if reinf_learning:
            
        score = loss_func_obs_stats(model, training_data_dict, real_obs=train_on_real_obs, mode=mode)
            
    else:

        y_pred = predict_points_local(model, training_data_dict, original_units=False, as_lists=False, mode=mode)
    #     diff = y_pred - self.training_data_dict['y_'+mode]
    #     squares = np.power(diff, 2) / np.shape(y_pred[0])
    #     weighted_squares = squares * self.training_data_dict[mode+'_weights'] / np.sum(self.training_data_dict[mode+'_weights'])
    #     score = np.sum(self.training_data_dict[mode+'_weights'])

        score = mean_squared_error(training_data_dict['y_'+mode], y_pred, training_data_dict[mode+'_weights'])
    #     print('score diff: {:.2e}'.format(score2 - score))

    return score

def predict_points_local(model, training_data_dict, original_units=True, as_lists=False, mode='test'):

    predicted_norm_points = model.predict(training_data_dict['input_{}_dict'.format(mode)])
    if type(predicted_norm_points) is list:
        predicted_norm_points = np.asarray(predicted_norm_points)
        predicted_norm_points = np.squeeze(predicted_norm_points, axis = -1)
        predicted_norm_points = np.transpose(predicted_norm_points)
        
    if original_units:
        predicted_points = convert_units(predicted_norm_points, training_data_dict['norm']['output'], 
                                         back_to_original=original_units, conv_values=training_data_dict['conv_values_output'])
    else:
        predicted_points = predicted_norm_points        
        
    return predicted_points
