# from torch_robotics.isaac_gym_envs.motion_planning_envs import PandaMotionPlanningIsaacGymEnv, MotionPlanningController
# from experiment_launcher import single_experiment_yaml, run_experiment
from mpd.models import ConditionedTemporalUnet, UNET_DIM_MULTS
from mpd.models.diffusion_models.sample_functions import guide_gradient_steps, ddpm_sample_fn, ddpm_cart_pole_sample_fn
from mpd.trainer import get_dataset, get_model
from mpd.utils.loading import load_params_from_yaml
from torch_robotics.torch_utils.seed import fix_random_seed
from torch_robotics.torch_utils.torch_timer import TimerCUDA
from torch_robotics.torch_utils.torch_utils import get_torch_device, freeze_torch_model_params

import mujoco
import mujoco.viewer
import numpy as np
import os
import matplotlib.pyplot as plt
import torch
import random
import casadi as ca
import time

# Trained Model Info
TRAINED_MODELS_DIR = '../../trained_models/' # main loader of all saved trained models
MODEL_FOLDER = 'panda_test6_117600'  #'180000_training_data' # choose a folder in the trained_models (eg. 420000 is the number of total training data, this folder contains all trained models based on the 420000 training data)
MODEL_PATH = '/root/cartpoleDiff/cart_pole_diffusion_based_on_MPD/trained_models/panda_test6_117600/final' #'/root/cartpoleDiff/cart_pole_diffusion_based_on_MPD/trained_models/180000_training_data/100000' # the absolute path of the trained model
MODEL_ID = 'final' # number of training
RESULTS_SAVED_PATH = '/root/cartpoleDiff/cart_pole_diffusion_based_on_MPD/model_performance_saving/Panda_test6_performance' # '/root/cartpoleDiff/cart_pole_diffusion_based_on_MPD/model_performance_saving/Panda_113400' 

# Sampling data
# NUM_SEED = 42
WEIGHT_GUIDANC = 0.01 # non-conditioning weight
HORIZON = 128
TARGET_POS = np.array([0.3, 0.3, 0.5]).reshape(3, 1)
SAMPLING_STEPS = 200
CONTROL_RATE = 10

Q = np.diag([10,10,10])
R = 0.5
P = np.diag([10,10,10])

def main():
    start_time = time.time()
    # memory 
    x_pos_memory = np.zeros((SAMPLING_STEPS, 3)) 
    q_pos_memory = np.zeros((SAMPLING_STEPS, 7)) 
    ctl_memory = np.zeros((SAMPLING_STEPS, 7))
    abs_dis_memory = np.zeros((SAMPLING_STEPS, 1))
    cost_memory = np.zeros((SAMPLING_STEPS, 1))

    # load model path
    model_dir = MODEL_PATH 
    results_dir = os.path.join(model_dir, 'results_inference')
    os.makedirs(results_dir, exist_ok=True)
    
    # load device data
    device: str = 'cuda'
    device = get_torch_device(device)
    tensor_args = {'device': device, 'dtype': torch.float32}

    # load trained model
    args = load_params_from_yaml(os.path.join(model_dir, "args.yaml"))
    
    # Load dataset
    train_subset, train_dataloader, val_subset, val_dataloader = get_dataset(
        dataset_class='InputsDataset',
        **args,
        tensor_args=tensor_args
    )
    dataset = train_subset.dataset
    n_support_points = dataset.n_support_points
    print(f'n_support_points -- {n_support_points}')
    print(f'state_dim -- {dataset.state_dim}')

    # Sampling initial data
    # ini_joint_states = sampling_data_generating()
    ini_joint_states = np.array([[0, 0, 0, 0, 0, 0, 0]])

    # panda mujoco
    panda = mujoco.MjModel.from_xml_path('/root/cartpoleDiff/cart_pole_diffusion_based_on_MPD/scripts/Panda/xml/mjx_scene.xml')
    data = mujoco.MjData(panda)
    # viewer = mujoco.viewer.launch(panda, data)

    # panda initialization
    data.qpos[:7] = ini_joint_states
    mujoco.mj_step(panda, data)
   
    # full initial state 20*1
    # q0_pos, x0_pos, x0 = generating_ini_state(panda, data, ini_joint_states)
    # q_pos_memory[0,:] = q0_pos.reshape(7)
    # x_pos_memory[0,:] = x0_pos.reshape(3)
    # print(f'initial x pos -- {x0[0,14:17]}')
    # print(f'xpos_0 --{x0_pos}')

    # load trained diffusion model
    model = loading_model(dataset, args, tensor_args, model_dir)

    # set initial conditioning info
    # x_current = x0.copy()
    single_time_set = np.zeros((SAMPLING_STEPS, 1))
    # initialize panda step
    sampling_step = 0

    # diffusion sampling loop
    for panda_step in range(0, SAMPLING_STEPS*CONTROL_RATE):

        if panda_step % CONTROL_RATE == 0:
            # current panda data loading
            q_current_pos, x_current_pos, context_current = state_loading(panda,data)
            
            # load context to cuda
            x_current = torch.tensor(context_current).to(device) 

            # data saving
            q_pos_memory[sampling_step,:] = q_current_pos.reshape(7)
            x_pos_memory[sampling_step,:] = x_current_pos.reshape(3)
            abs_dis_memory[sampling_step,:] = np.linalg.norm(x_current_pos - TARGET_POS)

            # sampling
            single_start = time.time()
            inputs_normalized_iters = diffusion_sampling(x_current, dataset, model, n_support_points)
            single_end = time.time()
            single_t = single_end - single_start 
            print(f'single time -- {single_t}')
            single_time_set[sampling_step,0] = single_t

            # last diffusion result unmormalize
            inputs_iters = dataset.unnormalize_states(inputs_normalized_iters)
            inputs_final = inputs_iters[-1] # 1 128 7
            print(f'control_policy -- {inputs_final.shape}')
            print(f'\n--------------------------------------\n')

            x_current = x_current.cpu() # copy cuda tensor at first to cpu
            # x0_array = np.squeeze(x_current.numpy()) # matrix (1*20) to vector (20)

            # horizon_inputs = np.zeros((1, HORIZON, 7))
            inputs_final = inputs_final.cpu()
            diffusion_predicted_states = diffusion_horizon_states(x_current_pos, inputs_final, q_current_pos)
            cost = mpc_cost(diffusion_predicted_states, inputs_final, Q, R, P)
            cost_memory[sampling_step,0] = cost

            # for n in range(0,HORIZON):
            #     horizon_inputs[0,n,:] = round(inputs_final[0,n,:].item(),4)
            # print(f'horizon_inputs -- {horizon_inputs}')
            applied_input_tensor = inputs_final[0,0,:]
            applied_input_array = applied_input_tensor.numpy()
            # applied_input = round(applied_input_array.item(),4) # retain 4 decimal places
            print(f'applied_input -- {applied_input_array}')
            ctl_memory[sampling_step,:] = applied_input_array 

            # Panda states updating
            data.ctrl[:7] = applied_input_array
            # mujoco.mj_step(panda, data)

            # q_current_pos, x_current_pos, x_next = state_updating(panda, data)
            # x_current = x_next
            print(f'current x pos -- {x_current[0,14:17]}')
            print(f'sampling step -- {sampling_step}')
            sampling_step = sampling_step + 1

        mujoco.mj_step(panda, data)
        if panda_step == SAMPLING_STEPS*CONTROL_RATE-1 :
            q_last_pos, x_last_pos, context_last = state_loading(panda,data)
            print(f'final panda x pos -- {x_last_pos}')
        
    end_time = time.time()
    delta_t_problem_solving = end_time - start_time

    # final position difference
    print(f'----------------------------------------------------')
    print(f'fianl abs distance -- {abs_dis_memory[-1,0]}')
    print(f'----------------------------------------------------')
    

    # single_time_set_array = np.array(single_time_set)


    ########################## plot ##########################
    # joint_name = 'qPOS' + str(ini_joint_states[0,0]) + '_' +  str(ini_joint_states[0,1]) + '_' + str(ini_joint_states[0,2]) + '_' + str(ini_joint_states[0,3]) + '_' + str(ini_joint_states[0,4]) + '_' + str(ini_joint_states[0,5]) + '_' + str(ini_joint_states[0,6]) + '_0test_pdf'
    joint_name = 'cost' + '_collecting_sample_16'
    figure_saving_path = os.path.join(RESULTS_SAVED_PATH, joint_name)
    os.makedirs(figure_saving_path, exist_ok=True)

    # save states trajectory
    x_trag_path =  os.path.join(figure_saving_path, f'x_trag' + '.npy')
    # print(f'x trag size -- {x_pos_memory}')
    np.save(x_trag_path, x_pos_memory)

    # save solving time 
    solving_time_path = os.path.join(figure_saving_path, f'solving_time_diffusion_' + '.npy')
    print(f'single time --{delta_t_problem_solving}')
    np.save(solving_time_path, delta_t_problem_solving)

    # save single time set
    single_time_path = os.path.join(figure_saving_path, f'single_time_diffusion_' + '.npy')
    # print(f'single time set  --{single_time_set}')
    np.save(single_time_path, single_time_set)

    # save cost 
    cost_sum = np.sum(cost_memory)
    cost_path = os.path.join(figure_saving_path, f'cost_' + '.npy')
    print(f'total cost --{cost_sum}')
    np.save(cost_path, cost_sum)

    T = SAMPLING_STEPS
    t = np.arange(0, T, 1)
    print(f't -- {len(t)}')

    ######## fig1 xpos 3D ########
    fig = plt.figure()
    ax = fig.add_subplot(projection='3d')
    ax.plot(x_pos_memory[:,0], x_pos_memory[:,1], x_pos_memory[:,2])

    # final & target point
    point = [x_pos_memory[-1,0], x_pos_memory[-1,1], x_pos_memory[-1,2]]
    ax.scatter(point[0], point[1], point[2], color='green', s=10)
    
    target = TARGET_POS
    ax.scatter(target[0,0], target[1,0], target[2,0], color='red', s=10)

    # Set axis labels
    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.set_zlabel('Z')

    ax.set_xlim([-1.5, 1.5])  # x-axis range
    ax.set_ylim([-1.5, 1.5])  # y-axis range
    ax.set_zlim([-1.5, 1.5])   # z-axis range
    ax.legend()

    figure_name = 'ini_state' + '_' + str(x_pos_memory[0,0]) + '_' + str(x_pos_memory[0,1]) + '_' + str(x_pos_memory[0,2]) + '_' + '_3d' + '.png'
    figure_path = os.path.join(figure_saving_path, figure_name)
    plt.savefig(figure_path)

    ######## fig2 qpos ########
    plt.figure()
    for i in range(7):
            plt.plot(t, q_pos_memory[:,i], label=f"Joint {i+1}")
    plt.xlabel("Sampling Step")
    plt.ylabel("Joint position [rad]")
    plt.legend()  

    figure_name = 'Joints trajectories' + '.pdf'
    figure_path = os.path.join(figure_saving_path, figure_name)
    plt.savefig(figure_path)  

    ######## fig3 ctrl ########
    plt.figure()
    for i in range(7):
            plt.plot(t, ctl_memory[:,i], label=f"u {i+1}")
    plt.xlabel("Sampling Step")
    plt.ylabel("Control inputs")
    plt.legend()  

    figure_name = 'Ctrls trajectories' + '.pdf'
    figure_path = os.path.join(figure_saving_path, figure_name)
    plt.savefig(figure_path)  

    ######## fig4 abs distance ########
    plt.figure()
    plt.plot(t, abs_dis_memory)
    plt.xlabel("Time [s]")
    plt.ylabel("absolute distance [m]")

    figure_name = 'Absolute distance trajectory' + '.pdf'
    figure_path = os.path.join(figure_saving_path, figure_name)
    plt.savefig(figure_path)  

    plt.show()




################################################################################################################################################

# initial sampling data generating
def sampling_data_generating():
    # np.random.seed(NUM_SEED)
    # random.seed(NUM_SEED)

    # Gaussian noise for initial states generating
    mean_ini = 0
    std_dev_ini = 0.1
    
    # sampling joint states generating
    sampling_ini_states_list = []

    gaussian_noise_ini_joint_1 = np.round(np.random.normal(mean_ini, std_dev_ini),2)
    gaussian_noise_ini_joint_2 = np.round(np.random.normal(mean_ini, std_dev_ini),2)
    gaussian_noise_ini_joint_3 = np.round(np.random.normal(mean_ini, std_dev_ini),2)
    gaussian_noise_ini_joint_4 = np.round(np.random.normal(mean_ini, std_dev_ini),2)
    gaussian_noise_ini_joint_5 = np.round(np.random.normal(mean_ini, std_dev_ini),2)
    gaussian_noise_ini_joint_6 = np.round(np.random.normal(mean_ini, std_dev_ini),2)
    sampling_ini_states_list.append([gaussian_noise_ini_joint_1,gaussian_noise_ini_joint_2,gaussian_noise_ini_joint_3,gaussian_noise_ini_joint_4,gaussian_noise_ini_joint_5,gaussian_noise_ini_joint_6, 0])
    sampling_ini_states = np.array(sampling_ini_states_list) # 1*7

    # random initial u guess
    sampling_u_guess_list = []

    u_guess_4 = np.round(random.uniform(-2, 2),2)
    u_guess_5 = np.round(random.uniform(-2, 2),2)
    u_guess_7 = np.round(random.uniform(-2, 2),2)
    sampling_u_guess_list.append([0,0,0,u_guess_4,u_guess_5,0,u_guess_7])
    sampling_ini_u_guess = np.array(sampling_u_guess_list) # 1*7

    return sampling_ini_states


# Jacobian Matrix
def compute_jacobian(model, data, tpoint):
    """Compute the Jacobian for the hand."""
    # Jacobian matrices for position (jacp) and orientation (jacr)
    jacp = np.zeros((3, model.nv))  # Position Jacobian
    jacr = np.zeros((3, model.nv))  # Orientation Jacobian
    
    body_id = 9

    # mujoco.mj_jacBody(model, data, jacp, jacr, body_id)
    mujoco.mj_jac(model, data, jacp, jacr, tpoint, body_id)
    
    return jacp, jacr


# generating initial state (20*1)
def generating_ini_state(panda, data, ini_joint_states):
    data.qpos[:7] = ini_joint_states
    mujoco.mj_step(panda, data)
    q_ini = np.array(data.qpos).reshape(-1, 1)
    q_dot_ini = np.array(data.qvel).reshape(-1, 1)
    x_ini = np.array(data.xpos[9,:]).reshape(-1, 1)
    
    # compute 'hand' x_dot 
    jacp, _ = compute_jacobian(panda, data, TARGET_POS)
    jacp = jacp[:, :7]
    x_dot_ini = ca.mtimes(jacp, q_dot_ini[:7])
   
    # full initial state 20*1
    x0 = np.zeros((20, 1))
    x0[:7] = q_ini[:7]
    x0[7:14] = q_dot_ini[:7]
    x0[14:17] = x_ini
    x0[17:20] = x_dot_ini
    
    q0_pos = q_ini[:7]
    x0_pos = x_ini

    x0 = x0.reshape(1,20)

    return q0_pos, x0_pos, x0


# state updating
def state_updating(panda, data):
    q_current = np.array(data.qpos).reshape(-1, 1)
    q_dot_current = np.array(data.qvel).reshape(-1, 1)
    x_current = np.array(data.xpos[9,:]).reshape(-1, 1)
    
    # compute 'hand' x_dot 
    jacp, _ = compute_jacobian(panda, data, TARGET_POS)
    jacp = jacp[:, :7]
    x_dot_current = ca.mtimes(jacp, q_dot_current[:7])
   
    # full initial state 20*1
    x_next= np.zeros((20, 1))
    x_next[:7] = q_current[:7]
    x_next[7:14] = q_dot_current[:7]
    x_next[14:17] = x_current
    x_next[17:20] = x_dot_current
    
    q_current_pos = q_current[:7]
    x_current_pos = x_current

    x_next = x_next.reshape(1,20)

    return q_current_pos, x_current_pos, x_next


# current context loading
def state_loading(panda,data):
    q_current = np.array(data.qpos).reshape(-1, 1)
    q_dot_current = np.array(data.qvel).reshape(-1, 1)
    x_current = np.array(data.xpos[9,:]).reshape(-1, 1)
    
    # compute 'hand' x_dot 
    jacp, _ = compute_jacobian(panda, data, TARGET_POS)
    jacp = jacp[:, :7]
    x_dot_current = ca.mtimes(jacp, q_dot_current[:7])
   
    # full initial state 20*1
    context_current= np.zeros((20, 1))
    context_current[:7] = q_current[:7]
    context_current[7:14] = q_dot_current[:7]
    context_current[14:17] = x_current
    context_current[17:20] = x_dot_current
    
    q_current_pos = q_current[:7]
    x_current_pos = x_current

    context_current = context_current.reshape(1,20)

    return q_current_pos, x_current_pos, context_current


# load trained model
def loading_model(dataset, args, tensor_args, model_dir):
    diffusion_configs = dict(
        variance_schedule=args['variance_schedule'],
        n_diffusion_steps=args['n_diffusion_steps'],
        predict_epsilon=args['predict_epsilon'],
    )
    unet_configs = dict(
        state_dim=dataset.state_dim,
        n_support_points=dataset.n_support_points,
        unet_input_dim=args['unet_input_dim'],
        dim_mults=UNET_DIM_MULTS[args['unet_dim_mults_option']],
    )
    diffusion_model = get_model(
        model_class=args['diffusion_model_class'],
        model=ConditionedTemporalUnet(**unet_configs),
        tensor_args=tensor_args,
        **diffusion_configs,
        **unet_configs
    )
    # 'ema_model_current_state_dict.pth'
    diffusion_model.load_state_dict(
        torch.load(os.path.join(model_dir, 'checkpoints', 'ema_model_current_state_dict.pth' if args['use_ema'] else 'model_current_state_dict.pth'),
        map_location=tensor_args['device'])
    )
    diffusion_model.eval()
    model = diffusion_model

    model = torch.compile(model)
    
    return model


# diffusion sampling
def diffusion_sampling(x_current, dataset, model, n_support_points):
    hard_conds = None
    context = dataset.normalize_condition(x_current)
    context_weight = WEIGHT_GUIDANC


    # sampling
    with TimerCUDA() as t_diffusion_time:
        inputs_normalized_iters = model.run_CFG(
            context, hard_conds, context_weight,
            n_samples=1, horizon=n_support_points,
            return_chain=True,
            sample_fn=ddpm_cart_pole_sample_fn,
            n_diffusion_steps_without_noise=5,
        )
    print(f't_model_sampling: {t_diffusion_time.elapsed:.4f} sec')
    # print(f'single_time: {t_diffusion_time} sec')
    print(inputs_normalized_iters.size()) # 31 1 128 7

    return inputs_normalized_iters


# diffusion cost
def mpc_cost(predicted_states, predicted_controls, Q, R, P):
    cost = 0
    predicted_controls = predicted_controls.numpy()
    predicted_controls = predicted_controls.transpose(2,1,0) # 1*128*7 --> 7*128*1 
    # initial cost
    x_0 = predicted_states[:,0,0]
    cost = Q[0,0]*(x_0[0]-TARGET_POS[0])**2 + Q[1,1]*(x_0[1]-TARGET_POS[1])**2 + Q[2,2]*(x_0[2]-TARGET_POS[2])**2

    # stage cost
    i = 0
    for i in range(predicted_controls.shape[1]-1):
        x_i = predicted_states[:,i+1,0]
        u_i_1 = predicted_controls[:,i,0]
        u_i = predicted_controls[:,i+1,0]
        delta_u = u_i - u_i_1
        # print(0.5*(ca.sumsqr(u_i)))
        cost += Q[0,0]*(x_i[0]-TARGET_POS[0])**2 + Q[1,1]*(x_i[1]-TARGET_POS[1])**2 + Q[2,2]*(x_i[2]-TARGET_POS[2])**2 + R*(ca.sumsqr(delta_u))

    # terminal cost
    x_end = predicted_states[:,-1,0]
    cost += P[0,0]*(x_end[0]-TARGET_POS[0])**2 + P[1,1]*(x_end[1]-TARGET_POS[1])**2 + P[2,2]*(x_end[2]-TARGET_POS[2])**2 # + 0.5*(ca.sumsqr(u_end-u_end_1))

    return cost

def diffusion_horizon_states(x_current_pos, inputs_final, q_current_pos):
    panda_cost = mujoco.MjModel.from_xml_path('/root/cartpoleDiff/cart_pole_diffusion_based_on_MPD/scripts/Panda/xml/mjx_scene.xml')
    data_cost = mujoco.MjData(panda_cost)
    data_cost.qpos[:7] = q_current_pos.reshape(7)
    mujoco.mj_step(panda_cost, data_cost)
     
    diffusion_horizon_states = np.zeros((3,HORIZON+1,1))

    x_current = np.array(data_cost.xpos[9,:]).reshape(-1, 1)
    diffusion_horizon_states[:,0,0] = x_current.reshape(3)
     
    for k in range(0,HORIZON):
        data_cost.ctrl[:7] = inputs_final[0,k,:].numpy()
        mujoco.mj_step(panda_cost, data_cost)
        x_current = np.array(data_cost.xpos[9,:]).reshape(-1, 1)
        diffusion_horizon_states[:,k+1,0] = x_current.reshape(3)

    return diffusion_horizon_states



if __name__ == "__main__":
      main()