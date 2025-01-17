import torch
torch.backends.cuda.matmul.allow_tf32 = True
import torch.nn as nn
import transformers
from utils import get_local_dir, get_local_run_dir, disable_dropout, init_distributed
import os
import hydra
import torch.distributed as dist
import torch.multiprocessing as mp
from omegaconf import OmegaConf, DictConfig
import trainers
import wandb
import json
import socket
from typing import Optional, Set
from peft import LoraConfig, get_peft_model, PeftModel, PeftConfig


OmegaConf.register_new_resolver("get_local_run_dir", lambda exp_name, local_dirs: get_local_run_dir(exp_name, local_dirs))

# to save memory when using reference model and adapters
class ModelWithDisabledAdapter:
    def __init__(self, model):
        self.model = model

    def __getattribute__(self, name):
        model = super().__getattribute__('model')

        attr = getattr(model, name)

        if callable(attr):
            def newfunc(*args, **kwargs):
                was_training = model.training  # Check if the model was in training mode
                model.eval()
                with model.disable_adapter():
                    result = attr(*args, **kwargs)
                if was_training:  # If the model was in training mode, switch back to it
                    model.train()  
                return result
            return newfunc
        else:
            return attr

    def __call__(self, *args, **kwargs):
        model = super().__getattribute__('model')
        was_training = model.training  # Check if the model was in training mode
        model.eval()  # Set model in evaluation mode
        with model.disable_adapter():
            result = model(*args, **kwargs)
        if was_training:  # If the model was in training mode, switch back to it
            model.train()  
        return result


def worker_main(rank: int, world_size: int, config: DictConfig, policy: nn.Module, reference_model: Optional[nn.Module] = None):
    """Main function for each worker process (may be only 1 for BasicTrainer/TensorParallelTrainer)."""
    if 'FSDP' in config.trainer:
        init_distributed(rank, world_size, port=config.fsdp_port)
    
    if config.debug:
        wandb.init = lambda *args, **kwargs: None
        wandb.log = lambda *args, **kwargs: None

    if rank == 0 and config.wandb.enabled:
        os.environ['WANDB_CACHE_DIR'] = get_local_dir(config.local_dirs)
        wandb.init(
            entity=config.wandb.entity,
            project=config.wandb.project,
            config=OmegaConf.to_container(config),
            dir=get_local_dir(config.local_dirs),
            name=config.exp_name,
        )

    TrainerClass = getattr(trainers, config.trainer)
    print(f'Creating trainer on process {rank} with world size {world_size}')
    trainer = TrainerClass(policy, config, config.seed, config.local_run_dir, reference_model=reference_model, rank=rank, world_size=world_size)

    trainer.train()
    trainer.save()


@hydra.main(version_base=None, config_path="config", config_name="config")
def main(config: DictConfig):
    """Main entry point for training. Validates config, creates/initializes model(s), and kicks off worker process(es)."""

    # Resolve hydra references, e.g. so we don't re-compute the run directory
    OmegaConf.resolve(config)

    missing_keys: Set[str] = OmegaConf.missing_keys(config)
    if missing_keys:
        raise ValueError(f"Got missing keys in config:\n{missing_keys}")

    if config.eval_every % config.batch_size != 0:
        print('WARNING: eval_every must be divisible by batch_size')
        print('Setting eval_every to', config.eval_every - config.eval_every % config.batch_size)
        config.eval_every = config.eval_every - config.eval_every % config.batch_size

    print(OmegaConf.to_yaml(config))

    config_path = os.path.join(config.local_run_dir, 'config.yaml')
    with open(config_path, 'w') as f:
        OmegaConf.save(config, f)

    print('=' * 80)
    print(f'Writing to {socket.gethostname()}:{config.local_run_dir}')
    print('=' * 80)
 
    os.environ['XDG_CACHE_HOME'] = get_local_dir(config.local_dirs)
    print('building policy')
    model_kwargs = {'device_map': 'balanced'} if config.trainer == 'BasicTrainer' else {}
    policy_dtype = getattr(torch, config.model.policy_dtype)
    policy = transformers.AutoModelForCausalLM.from_pretrained(
        config.model.name_or_path, cache_dir=get_local_dir(config.local_dirs), low_cpu_mem_usage=True, torch_dtype=policy_dtype, **model_kwargs)
    disable_dropout(policy)
    
    if config.lora.enabled:
        print('-------------------adding LORA')
        target_modules= [
            "q_proj",
            "v_proj",
            "k_proj",
            "o_proj",
        ]
        lora_config = LoraConfig(
            r=config.lora.lora_r,
            lora_alpha=config.lora.lora_alpha,
            lora_dropout=config.lora.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules = target_modules
        )

        policy = get_peft_model(policy, lora_config)

    if config.loss.name == 'dpo':
        print('building reference model')
        if config.lora.enabled:
            reference_model = ModelWithDisabledAdapter(policy)
        else:
            reference_model_dtype = getattr(torch, config.model.reference_dtype)
            reference_model = transformers.AutoModelForCausalLM.from_pretrained(
                config.model.name_or_path, cache_dir=get_local_dir(config.local_dirs), low_cpu_mem_usage=True, torch_dtype=reference_model_dtype, **model_kwargs)
            disable_dropout(reference_model)
    else:
        reference_model = None

    if config.model.archive is not None:
        state_dict = torch.load(config.model.archive+'/policy.pt', map_location='cpu')
        step, metrics = state_dict['step_idx'], state_dict['metrics']
        print(f'loading pre-trained weights at step {step} from {config.model.archive}/policy.pt with metrics {json.dumps(metrics, indent=2)}')
        if config.lora.enabled:
            policy = transformers.AutoModelForCausalLM.from_pretrained(
                config.model.name_or_path, cache_dir=get_local_dir(config.local_dirs), low_cpu_mem_usage=True, torch_dtype=policy_dtype, **model_kwargs)
            disable_dropout(policy)
            policy = PeftModel.from_pretrained(policy, config.model.archive, is_trainable=True)
            if config.loss.name == 'dpo':
                reference_model = ModelWithDisabledAdapter(policy)
        else:
            policy.load_state_dict(state_dict['state'])
            if config.loss.name == 'dpo':
                reference_model.load_state_dict(state_dict['state'])
        print('loaded pre-trained weights')
    
    if 'FSDP' in config.trainer:
        world_size = torch.cuda.device_count()
        print('starting', world_size, 'processes for FSDP training')
        mp.spawn(worker_main, nprocs=world_size, args=(world_size, config, policy, reference_model), join=True)
    else:
        print('starting single-process worker')
        worker_main(0, 1, config, policy, reference_model)


if __name__ == '__main__':
    main()
