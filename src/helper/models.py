from torch import Tensor
from torch.nn import Module
from torch.optim.optimizer import Optimizer


class DomainModelConfig(object):
    def __init__(
        self,
        model: Module,
        optimizer: Optimizer,
        recon_loss_function: Module,
        inputs: Tensor = None,
        labels: Tensor = None,
        trainable: bool = True,
    ):
        self.model = model
        self.optimizer = optimizer
        self.recon_loss_function = recon_loss_function
        self.inputs = inputs
        self.labels = labels
        self.trainable = trainable

        self.model.recon_loss_module = recon_loss_function

    def reset_model(self):
        self.model.load_state_dict(self.initial_weights)


class DomainConfig(object):
    def __init__(
        self,
        name: str,
        model: Module,
        optimizer: Optimizer,
        recon_loss_function: Module,
        data_loader_dict: dict,
        data_key: str,
        label_key: str,
        train_model: bool = True,
    ):
        self.name = name
        self.domain_model_config = DomainModelConfig(
            model=model,
            optimizer=optimizer,
            recon_loss_function=recon_loss_function,
            trainable=train_model,
        )
        self.data_loader_dict = data_loader_dict
        self.data_key = data_key
        self.label_key = label_key
