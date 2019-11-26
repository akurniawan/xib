from dev_misc.arglib import g, parse_args, show_args
from dev_misc.devlib import initiate
from dev_misc.devlib.named_tensor import patch_named_tensors
from xib.cfg import reg
from xib.training.manager import DecipherManager, LMManager, TransferManager


def train():
    if g.task == 'lm':
        manager = LMManager()
    # elif g.task == 'adapt':
    #     manager = AdaptManager()
    elif g.task == 'transfer':
        manager = TransferManager()
    else:
        manager = DecipherManager()
    manager.run()


if __name__ == "__main__":
    initiate(reg, logger=True, log_dir=True, log_level=True, gpus=True, random_seed=True, commit_id=True)
    patch_named_tensors()

    parse_args()
    show_args()

    train()
