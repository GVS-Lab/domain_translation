import copy
import logging
import os
import time
from typing import List, Tuple

import numpy as np
import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import Module
from torch.optim import Adam
from torch.optim.optimizer import Optimizer

from src.functions.loss_functions import compute_KL_loss
from src.functions.metrics import accuracy
from src.helper.models import DomainModelConfig, DomainConfig
from src.utils.torch.general import get_device
from src.utils.torch.visualization import (
    visualize_image_translation_performance,
    visualize_image_vae_performance,
)


def train_autoencoders_two_domains(
    domain_model_configurations: List[DomainModelConfig],
    latent_dcm: Module,
    latent_dcm_loss: Module,
    latent_clf: Module = None,
    latent_clf_optimizer: Optimizer = None,
    latent_clf_loss: Module = None,
    alpha: float = 0.1,
    beta: float = 1.0,
    lamb: float = 0.00000001,
    device: str = "cuda:0",
    use_dcm: bool = True,
    use_clf: bool = True,
    phase: str = "train",
) -> dict:
    # Expects 2 model configurations (one for each domain)
    model_configuration_i = domain_model_configurations[0]
    model_configuration_j = domain_model_configurations[1]

    # Get all parameters of the configuration for domain i
    vae_i = model_configuration_i.model
    optimizer_i = model_configuration_i.optimizer
    inputs_i = model_configuration_i.inputs
    labels_i = model_configuration_i.labels
    recon_loss_fct_i = model_configuration_i.recon_loss_function
    train_i = model_configuration_i.train

    # Get all parameters of the configuration for domain j
    vae_j = model_configuration_j.model
    optimizer_j = model_configuration_j.optimizer
    inputs_j = model_configuration_j.inputs
    labels_j = model_configuration_j.labels
    recon_loss_fct_j = model_configuration_j.recon_loss_function
    train_j = model_configuration_j.train

    # Set VAE models to train if defined in respective configuration
    if phase == "train" and train_i:
        vae_i.train()
    else:
        vae_i.eval()

    vae_i.to(device)
    vae_i.zero_grad()

    if phase == "train" and train_j:
        vae_j.train()
    else:
        vae_j.eval()

    vae_j.to(device)
    vae_j.zero_grad()

    # The discriminator will not be trained but only used to compute the adversarial loss for the AE updates
    latent_dcm.eval()
    latent_dcm.to(device)

    if use_clf:
        assert latent_clf is not None
        if phase == "train":
            latent_clf.train()
        else:
            latent_clf.eval()
        latent_clf.to(device)
        latent_clf.zero_grad()

    # Forward pass of the VAE
    inputs_i, inputs_j = inputs_i.to(device), inputs_j.to(device)
    recons_i, latents_i, mu_i, logvar_i = vae_i(inputs_i)
    recons_j, latents_j, mu_j, logvar_j = vae_j(inputs_j)

    if use_dcm:
        labels_i, labels_j = (
            labels_i.to(device),
            labels_j.to(device),
        )

        # Add class label to latent representations to ensure that latent representations encode generic information
        # independent from the used data modality (see Adversarial AutoEncoder paper)
        dcm_input_i = torch.cat(
            (latents_i, labels_i.float().view(-1, 1).expand(-1, 1)), dim=1
        )
        dcm_input_j = torch.cat(
            (latents_j, labels_j.float().view(-1, 1).expand(-1, 1)), dim=1
        )

    else:
        dcm_input_i = latents_i
        dcm_input_j = latents_j

    dcm_output_i = latent_dcm(dcm_input_i)
    dcm_output_j = latent_dcm(dcm_input_j)

    domain_labels_i = torch.zeros(dcm_output_i.size(0)).long().to(device)
    domain_labels_j = torch.ones(dcm_output_j.size(0)).long().to(device)

    # Forward pass latent classifier if it is supposed to be trained and used to assess the integration of the learned
    # latent spaces
    if use_clf:
        clf_output_i = latent_clf(latents_i)
        clf_output_j = latent_clf(latents_j)

    # Compute losses

    recon_loss_i = recon_loss_fct_i(recons_i, inputs_i)
    recon_loss_j = recon_loss_fct_j(recons_j, inputs_j)

    # kl_loss = kl_divergence(mu_i, logvar_i) + kl_divergence(mu_j, logvar_j)
    kl_loss = compute_KL_loss(mu_i, logvar_i) + compute_KL_loss(mu_j, logvar_j)
    kl_loss *= lamb

    # Calculate adversarial loss - by mixing labels indicating domain with output predictions to "confuse" the
    # discriminator and encourage learning autoencoder that make the distinction between the modalities in the latent
    # space as difficult as possible for the discriminator

    # Todo remove .item() from first term
    dcm_loss = 0.5 * latent_dcm_loss(
        dcm_output_i, domain_labels_j
    ) + 0.5 * latent_dcm_loss(dcm_output_j, domain_labels_i)

    total_loss = alpha * (recon_loss_i + recon_loss_j) + kl_loss + dcm_loss

    # Add loss of latent classifier if this is trained
    if use_clf:
        clf_loss = 0.5 * (
            latent_clf_loss(clf_output_i, labels_i)
            + latent_clf_loss(clf_output_j, labels_j)
        )
        clf_loss *= beta
        total_loss += clf_loss

    # Backpropagate loss and update parameters if we are in the training phase
    if phase == "train":
        total_loss.backward()
        if train_i:
            optimizer_i.step()
            vae_i.updated = True
        if train_j:
            optimizer_j.step()
            vae_j.updated = True
        if use_clf:
            latent_clf_optimizer.step()

    # Get summary statistics
    batch_size_i = inputs_i.size(0)
    batch_size_j = inputs_j.size(0)
    latent_size_i = mu_i.size(0)
    latent_size_j = mu_j.size(0)
    summary_stats = {
        "recon_loss_i": recon_loss_i.item() * batch_size_i,
        "recon_loss_j": recon_loss_j.item() * batch_size_j,
        "dcm_loss": dcm_loss.item() * (batch_size_i + batch_size_j),
        "kl_loss": kl_loss.item() * (latent_size_i + latent_size_j),
        "total_loss": total_loss.item(),
    }
    if use_clf:
        summary_stats["clf_loss"] = clf_loss.item()

    del recon_loss_i
    del recon_loss_j
    del dcm_loss
    del total_loss
    del kl_loss
    del domain_labels_i
    del domain_labels_j
    del dcm_input_i
    del dcm_input_j
    del dcm_output_i
    del dcm_output_j
    del latents_i
    del latents_j
    del inputs_i
    del inputs_j
    if use_dcm:
        del labels_i
        del labels_j
    del recons_i
    del recons_j
    del mu_i
    del mu_j
    del logvar_i
    del logvar_j

    return summary_stats


def train_latent_dcm_two_domains(
    domain_model_configurations: List[DomainModelConfig],
    latent_dcm: nn.Module,
    latent_dcm_optimizer: Optimizer,
    latent_dcm_loss: Module,
    use_dcm: bool,
    device: str = "cuda:0",
    phase: str = "train",
) -> dict:
    # Get the model configurations for the two domains
    model_configuration_i = domain_model_configurations[0]
    model_configuration_j = domain_model_configurations[1]

    # Get all parameters of the configuration for domain i
    vae_i = model_configuration_i.model
    inputs_i = model_configuration_i.inputs
    labels_i = model_configuration_i.labels

    # Get all parameters of the configuration for domain j
    vae_j = model_configuration_j.model
    inputs_j = model_configuration_j.inputs
    labels_j = model_configuration_j.labels

    # Set VAE models to eval for the training of the discriminator
    vae_i.eval()
    vae_j.eval()

    # Send models and data to device
    inputs_i, inputs_j = inputs_i.to(device), inputs_j.to(device)

    vae_i.to(device)
    vae_j.to(device)

    # Set latent discriminator to train and reset the parameters if in phase train
    if phase == "train":
        latent_dcm.train()
        latent_dcm.zero_grad()

    # Send the discriminator to the device
    latent_dcm.to(device)

    # Forward pass
    _, latents_i, _, _ = vae_i(inputs_i)
    _, latents_j, _, _ = vae_j(inputs_j)

    if use_dcm:
        labels_i, labels_j = (
            Variable(labels_i).to(device),
            Variable(labels_j).to(device),
        )

        # Add class label to latent representations to ensure that latent representations encode generic information
        # independent from the used data modality (see Adversarial AutoEncoder paper)
        dcm_input_i = torch.cat(
            (latents_i, labels_i.float().view(-1, 1).expand(-1, 1)), dim=1
        )
        dcm_input_j = torch.cat(
            (latents_j, labels_j.float().view(-1, 1).expand(-1, 1)), dim=1
        )

    else:
        dcm_input_i = latents_i
        dcm_input_j = latents_j

    dcm_output_i = latent_dcm(dcm_input_i)
    dcm_output_j = latent_dcm(dcm_input_j)

    domain_labels_i = torch.zeros(dcm_output_i.size(0)).long().to(device)
    domain_labels_j = torch.ones(dcm_output_j.size(0)).long().to(device)

    dcm_loss = 0.5 * (
        latent_dcm_loss(dcm_output_i, domain_labels_i)
        + latent_dcm_loss(dcm_output_j, domain_labels_j)
    )

    # Backpropagate loss and update parameters if in phase 'train'
    if phase == "train":
        dcm_loss.backward()
        latent_dcm_optimizer.step()

    # Get summary statistics
    batch_size_i = inputs_i.size(0)
    batch_size_j = inputs_j.size(0)

    accuracy_i = accuracy(dcm_output_i, domain_labels_i)
    accuracy_j = accuracy(dcm_output_j, domain_labels_j)

    summary_stats = {
        "dcm_loss": dcm_loss.item() * (batch_size_i + batch_size_j),
        "accuracy_i": accuracy_i,
        "accuracy_j": accuracy_j,
    }

    del dcm_loss
    del domain_labels_i
    del domain_labels_j
    del dcm_output_i
    del dcm_output_j
    del latents_i
    del latents_j
    del inputs_i
    del inputs_j
    if use_dcm:
        del labels_i
        del labels_j

    return summary_stats


def process_epoch_two_domains(
    domain_configs: List[DomainConfig],
    latent_dcm: Module,
    latent_dcm_optimizer: Optimizer,
    latent_dcm_loss: Module,
    latent_clf: Module = None,
    latent_clf_optimizer: Optimizer = None,
    latent_clf_loss: Module = None,
    alpha: float = 0.1,
    beta: float = 1.0,
    lamb: float = 0.00000001,
    use_dcm: bool = True,
    use_clf: bool = False,
    phase: str = "train",
    device: str = "cuda:0",
) -> dict:
    # Get domain configurations for the two domains
    domain_config_i = domain_configs[0]
    domain_model_config_i = domain_config_i.domain_model_config
    data_loader_dict_i = domain_config_i.data_loader_dict
    train_loader_i = data_loader_dict_i[phase]
    data_key_i = domain_config_i.data_key
    label_key_i = domain_config_i.label_key

    domain_config_j = domain_configs[1]
    domain_model_config_j = domain_config_j.domain_model_config
    data_loader_dict_j = domain_config_j.data_loader_dict
    train_loader_j = data_loader_dict_j[phase]
    data_key_j = domain_config_j.data_key
    label_key_j = domain_config_j.label_key

    # Initialize epoch statistics
    recon_loss_i = 0
    recon_loss_j = 0
    dcm_loss = 0
    clf_loss = 0
    ae_dcm_loss = 0
    kl_loss = 0
    total_loss = 0

    correct_preds_i = 0
    n_preds_i = 0
    correct_preds_j = 0
    n_preds_j = 0

    # Iterate over batches
    for index, (samples_i, samples_j) in enumerate(zip(train_loader_i, train_loader_j)):
        # Set model_configs
        domain_model_config_i.inputs = samples_i[data_key_i]
        domain_model_config_i.labels = samples_i[label_key_i]

        domain_model_config_j.inputs = samples_j[data_key_j]
        domain_model_config_j.labels = samples_j[label_key_j]

        domain_model_configs = [domain_model_config_i, domain_model_config_j]

        ae_train_summary = train_autoencoders_two_domains(
            domain_model_configurations=domain_model_configs,
            latent_dcm=latent_dcm,
            latent_dcm_loss=latent_dcm_loss,
            latent_clf=latent_clf,
            latent_clf_loss=latent_clf_loss,
            latent_clf_optimizer=latent_clf_optimizer,
            alpha=alpha,
            beta=beta,
            lamb=lamb,
            use_dcm=use_dcm,
            use_clf=use_clf,
            phase=phase,
            device=device,
        )
        # Update statistics after training the AE
        recon_loss_i += ae_train_summary["recon_loss_i"]
        recon_loss_j += ae_train_summary["recon_loss_j"]
        ae_dcm_loss += ae_train_summary["dcm_loss"]
        total_loss += ae_train_summary["total_loss"]
        kl_loss += ae_train_summary["kl_loss"]

        if use_clf:
            assert latent_clf is None
            clf_loss += ae_train_summary["clf_loss"]

        clf_train_summary = train_latent_dcm_two_domains(
            domain_model_configurations=domain_model_configs,
            latent_dcm=latent_dcm,
            latent_dcm_loss=latent_dcm_loss,
            latent_dcm_optimizer=latent_dcm_optimizer,
            use_dcm=use_dcm,
            phase=phase,
            device=device,
        )

        # Update statistics after training the DCM:
        dcm_loss += clf_train_summary["dcm_loss"]
        correct_preds_i += clf_train_summary["accuracy_i"][0]
        n_preds_i += clf_train_summary["accuracy_i"][1]
        correct_preds_j += clf_train_summary["accuracy_j"][0]
        n_preds_j += clf_train_summary["accuracy_j"][1]

    # Get average over batches for statistics
    recon_loss_i /= n_preds_i
    recon_loss_j /= n_preds_j

    dcm_loss /= n_preds_i + n_preds_j
    ae_dcm_loss /= n_preds_i + n_preds_j

    kl_loss /= n_preds_i + n_preds_j

    total_loss /= n_preds_i + n_preds_j

    accuracy_i = correct_preds_i / n_preds_i
    accuracy_j = correct_preds_j / n_preds_j

    epoch_statistics = {
        "recon_loss_i": recon_loss_i,
        "recon_loss_j": recon_loss_j,
        "dcm_loss": dcm_loss,
        "ae_dcm_loss": ae_dcm_loss,
        "kl_loss": kl_loss,
        "accuracy_i": accuracy_i,
        "accuracy_j": accuracy_j,
        "total_loss": total_loss,
    }

    if use_clf:
        clf_loss /= n_preds_i + n_preds_j
        epoch_statistics["clf_loss"] = clf_loss

    return epoch_statistics


def train_val_test_loop_two_domains(
    output_dir: str,
    domain_configs: List[DomainConfig],
    latent_dcm_config: dict = None,
    latent_clf_config: dict = None,
    alpha: float = 0.1,
    beta: float = 1.0,
    lamb: float = 0.00000001,
    use_dcm: bool = True,
    use_clf: bool = False,
    num_epochs: int = 500,
    save_freq: int = 10,
    early_stopping: int = 20,
    device: str = None,
) -> Tuple[dict, dict]:
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Get available device, if cuda is available the GPU will be used
    if not device:
        device = get_device()

    # Store start time of the training
    start_time = time.time()

    # Initialize early stopping counter
    es_counter = 0
    if early_stopping < 0:
        early_stopping = num_epochs

    total_loss_dict = {"train": [], "val": []}

    # Reserve space to save best model configurations
    domain_names = [domain_configs[0].name, domain_configs[1].name]
    best_vae_i_weights = domain_configs[0].domain_model_config.model.cpu().state_dict()
    best_vae_j_weights = domain_configs[1].domain_model_config.model.cpu().state_dict()
    best_model_configs = {
        "vae_i_weights": best_vae_i_weights,
        "vae_j_weights": best_vae_j_weights,
    }

    for i in range(len(domain_names)):
        logging.debug("VAE for domain {}:".format(domain_names[i]))
        logging.debug(domain_configs[i].domain_model_config.model)

    # Unpack latent model configurations
    if latent_dcm_config is not None:
        latent_dcm = latent_dcm_config["model"]
        logging.debug("Latent discriminator:")
        logging.debug(latent_dcm)
        latent_dcm_optimizer = latent_dcm_config["optimizer"]
        latent_dcm_loss = latent_dcm_config["loss"]
    else:
        latent_dcm = None
        latent_dcm_optimizer = None
        latent_dcm_loss = None

    if latent_clf_config is not None:
        latent_clf = latent_clf_config["model"]
        logging.debug("Latent classifier")
        logging.debug(latent_clf)
        latent_clf_optimizer = latent_clf_config["optimizer"]
        latent_clf_loss = latent_clf_config["loss"]
    else:
        latent_clf = None
        latent_clf_optimizer = None
        latent_clf_loss = None

    if latent_dcm is not None:
        best_latent_dcm_weights = latent_dcm.state_dict()
        best_model_configs["dcm_weights"] = best_latent_dcm_weights

    if latent_clf is not None:
        best_latent_clf_weights = latent_clf.state_dict()
        best_model_configs["clf_weights"] = best_latent_clf_weights

    # Initialize current best loss
    best_total_loss = np.infty

    # Iterate over the epochs
    for i in range(num_epochs):
        logging.debug("---" * 20)
        logging.debug("---" * 20)
        logging.debug("Started epoch {}/{}".format(i + 1, num_epochs))
        logging.debug("---" * 20)

        # Check if early stopping is triggered
        if es_counter > early_stopping:
            logging.debug(
                "Training was stopped early due to no improvement of the validation"
                " loss for {} epochs.".format(early_stopping)
            )
            break

        # Iterate over training and validation phase
        for phase in ["train", "val"]:

            epoch_statistics = process_epoch_two_domains(
                domain_configs=domain_configs,
                latent_dcm=latent_dcm,
                latent_dcm_optimizer=latent_dcm_optimizer,
                latent_dcm_loss=latent_dcm_loss,
                latent_clf=latent_clf,
                latent_clf_optimizer=latent_clf_optimizer,
                latent_clf_loss=latent_clf_loss,
                alpha=alpha,
                beta=beta,
                lamb=lamb,
                use_dcm=use_dcm,
                use_clf=use_clf,
                phase=phase,
                device=device,
            )

            logging.debug(
                "{} LOSS STATISTICS FOR EPOCH {}: ".format(phase.upper(), i + 1)
            )

            logging.debug(
                "Reconstruction Loss for {} domain: {:.8f}".format(
                    domain_names[0], epoch_statistics["recon_loss_i"]
                )
            )
            logging.debug(
                "Accuracy of DCM for {} domain: {:.8f}".format(
                    domain_names[0], epoch_statistics["accuracy_i"]
                )
            )

            logging.debug(
                "Reconstruction Loss for {} domain: {:.8f}".format(
                    domain_names[1], epoch_statistics["recon_loss_j"]
                )
            )
            logging.debug(
                "Accuracy of DCM for {} domain: {:.8f}".format(
                    domain_names[1], epoch_statistics["accuracy_j"]
                )
            )

            logging.debug(
                "Latent autoencoder discriminator loss: {:.8f}".format(
                    epoch_statistics["ae_dcm_loss"]
                )
            )
            logging.debug(
                "Latent discriminator loss: {:.8f}".format(epoch_statistics["dcm_loss"])
            )

            logging.debug(
                "Latent kld regularizer loss: {:.8f}".format(
                    epoch_statistics["kl_loss"]
                )
            )

            if "clf_loss" in epoch_statistics:
                logging.debug(
                    "Latent classifier loss: {:.8f}".format(
                        epoch_statistics["clf_loss"]
                    )
                )

            logging.debug("***" * 20)

            epoch_total_loss = epoch_statistics["total_loss"]
            logging.debug("Total {} loss: {:.8f}".format(phase, epoch_total_loss))
            logging.debug("***" * 20)

            total_loss_dict[phase].append(epoch_total_loss)

            if phase == "val":
                # Save model states if current parameters give the best validation loss
                if epoch_total_loss < best_total_loss:
                    es_counter = 0
                    best_total_loss = epoch_total_loss

                    best_vae_i_weights = copy.deepcopy(
                        domain_configs[0].domain_model_config.model.cpu().state_dict()
                    )
                    best_vae_j_weights = copy.deepcopy(
                        domain_configs[1].domain_model_config.model.cpu().state_dict()
                    )

                    best_model_configs["best_vae_i_weights"] = best_vae_i_weights
                    best_model_configs["best_vae_j_weights"] = best_vae_j_weights

                    torch.save(
                        best_vae_i_weights,
                        "{}/best_vae_{}.pth".format(output_dir, domain_names[0]),
                    )
                    torch.save(
                        best_vae_j_weights,
                        "{}/best_vae_{}.pth".format(output_dir, domain_names[1]),
                    )

                    if latent_dcm is not None:
                        best_latent_dcm_weights = copy.deepcopy(
                            latent_dcm.cpu().state_dict()
                        )
                        best_model_configs["dcm_weights"] = best_latent_dcm_weights
                        torch.save(
                            best_latent_dcm_weights,
                            "{}/best_dcm.pth".format(output_dir),
                        )
                    if latent_clf is not None:
                        best_latent_clf_weights = copy.deepcopy(
                            latent_clf.cpu().state_dict()
                        )
                        best_model_configs["clf_weights"] = best_latent_clf_weights
                        torch.save(
                            best_latent_clf_weights,
                            "{}/best_clf.pth".format(output_dir),
                        )
                else:
                    es_counter += 1

                if i % save_freq == 0:

                    domain_model_configs = [
                        domain_configs[0].domain_model_config,
                        domain_configs[1].domain_model_config,
                    ]
                    visualize_image_translation_performance(
                        domain_model_configs=domain_model_configs,
                        epoch=i,
                        output_dir=output_dir,
                        device=device,
                    )

                    # Save model states regularly
                    checkpoint_dir = "{}/epoch_{}".format(output_dir, i + 1)
                    os.makedirs(checkpoint_dir, exist_ok=True)

                    vae_i_weights = (
                        domain_configs[0].domain_model_config.model.cpu().state_dict()
                    )
                    vae_j_weights = (
                        domain_configs[1].domain_model_config.model.cpu().state_dict()
                    )
                    torch.save(
                        vae_i_weights,
                        "{}/vae_{}.pth".format(checkpoint_dir, domain_names[0]),
                    )
                    torch.save(
                        vae_j_weights,
                        "{}/vae_{}.pth".format(checkpoint_dir, domain_names[1]),
                    )

                    if latent_dcm is not None:
                        latent_dcm_weights = latent_dcm.cpu().state_dict()
                        torch.save(
                            latent_dcm_weights, "{}/dcm.pth".format(checkpoint_dir)
                        )
                    if latent_clf is not None:
                        latent_clf_weights = latent_clf.cpu().state_dict()
                        torch.save(
                            latent_clf_weights, "{}/clf.pth".format(checkpoint_dir)
                        )

    # Training complete
    time_elapsed = time.time() - start_time

    logging.debug("###" * 20)
    logging.debug(
        "Training completed in {:.0f}m {:0f}s".format(
            time_elapsed // 60, time_elapsed % 60
        )
    )

    # Load best models
    domain_configs[0].domain_model_config.model.load_state_dict(best_vae_i_weights)
    domain_configs[1].domain_model_config.model.load_state_dict(best_vae_j_weights)

    if latent_dcm is not None:
        latent_dcm.load_state_dict(best_latent_dcm_weights)
    if latent_clf is not None:
        latent_clf.load_state_dict(best_latent_clf_weights)

    if "test" in domain_configs[0].data_loader_dict:
        epoch_statistics = process_epoch_two_domains(
            domain_configs=domain_configs,
            latent_dcm=latent_dcm,
            latent_dcm_optimizer=latent_dcm_optimizer,
            latent_dcm_loss=latent_dcm_loss,
            latent_clf=latent_clf,
            latent_clf_optimizer=latent_clf_optimizer,
            latent_clf_loss=latent_clf_loss,
            alpha=alpha,
            use_dcm=use_dcm,
            use_clf=use_clf,
            phase="test",
            device=device,
        )

        logging.debug("###" * 20)
        logging.debug("TEST LOSS STATISTICS: ")

        logging.debug(
            "Reconstruction Loss for {} domain: {:.8f}".format(
                domain_names[0], epoch_statistics["recon_loss_i"]
            )
        )
        logging.debug(
            "Accuracy of DCM for {} domain: {:.8f}".format(
                domain_names[0], epoch_statistics["accuracy_i"]
            )
        )

        logging.debug(
            "Reconstruction Loss for {} domain: {:.8f}".format(
                domain_names[1], epoch_statistics["recon_loss_j"]
            )
        )
        logging.debug(
            "Accuracy of DCM for {} domain: {:.8f}".format(
                domain_names[1], epoch_statistics["accuracy_j"]
            )
        )

        logging.debug(
            "Latent autoencoder discriminator loss: {:.8f}".format(
                epoch_statistics["ae_dcm_loss"]
            )
        )
        logging.debug(
            "Latent discriminator loss: {:.8f}".format(epoch_statistics["dcm_loss"])
        )
        if "clf_loss" in epoch_statistics:
            logging.debug(
                "Latent classifier loss: {:.8f}".format(epoch_statistics["clf_loss"])
            )

        logging.debug("***" * 20)

        epoch_total_loss = epoch_statistics["total_loss"]
        logging.debug("Total test loss: {:.8f}".format(epoch_total_loss))

        logging.debug("***" * 20)

    # Summarize return parameters
    trained_models = {
        "vae_i": domain_configs[0].domain_model_config.model,
        "vae_j": domain_configs[1].domain_model_config.model,
    }
    if latent_dcm is not None:
        trained_models["dcm"] = latent_dcm
    if latent_clf is not None:
        trained_models["clf"] = latent_clf

    return trained_models, total_loss_dict


def train_autoencoder(
    domain_model_config: DomainModelConfig,
    latent_clf: Module,
    latent_clf_optimizer: Optimizer,
    latent_clf_loss: Module,
    beta: float = 0.001,
    lamb: float = 1e-8,
    phase: str = "train",
    use_clf: bool = True,
    device: str = "cuda:0",
) -> dict:
    # Get all parameters of the configuration for domain i
    vae = domain_model_config.model
    optimizer = domain_model_config.optimizer
    inputs = domain_model_config.inputs
    labels = domain_model_config.labels
    recon_loss_fct = domain_model_config.recon_loss_function
    train = domain_model_config.train

    # Set VAE model to train if defined in respective configuration
    if phase == "train" and train:
        vae.train()
    else:
        vae.eval()

    vae.to(device)
    vae.zero_grad()

    if use_clf:
        assert latent_clf is not None
        if phase == "train":
            latent_clf.train()
        else:
            latent_clf.eval()
        latent_clf.to(device)
        latent_clf.zero_grad()

    # Forward pass of the VAE
    inputs = inputs.to(device)
    labels = labels.to(device)
    recons, latents, mu, logvar = vae(inputs)

    # Forward pass latent classifier if it is supposed to be trained and used to assess the integration of the learned
    # latent spaces
    if use_clf:
        clf_output = latent_clf(latents)

    # Compute losses

    recon_loss = recon_loss_fct(recons, inputs)
    kl_loss = compute_KL_loss(mu, logvar)
    kl_loss *= lamb

    total_loss = recon_loss + kl_loss

    # Add loss of latent classifier if this is trained
    if use_clf:
        clf_loss = latent_clf_loss(clf_output, labels.view(-1).long())
        clf_loss *= beta
        total_loss += clf_loss

    # Backpropagate loss and update parameters if we are in the training phase
    if phase == "train":
        total_loss.backward()
        if train:
            optimizer.step()
            vae.updated = True

        if use_clf:
            latent_clf_optimizer.step()

    # Get summary statistics
    batch_size = inputs.size(0)
    latent_size = mu.size(0)

    batch_statistics = {
        "recon_loss": recon_loss.item() * batch_size,
        "kl_loss": kl_loss.item() * latent_size,
    }
    total_loss_item = recon_loss.item() * batch_size + kl_loss.item() * latent_size
    if use_clf:
        batch_statistics["clf_loss"] = clf_loss.item() * batch_size
        batch_statistics["accuracy"] = accuracy(clf_output, labels)
        total_loss_item += clf_loss.item() * batch_size
    batch_statistics["total_loss"] = total_loss_item

    del recon_loss
    del total_loss
    del kl_loss
    del latents
    del inputs
    del labels
    del recons
    del mu
    del logvar

    return batch_statistics


def process_epoch_single_domain(
    domain_config: DomainConfig,
    latent_clf: Module = None,
    latent_clf_optimizer: Optimizer = None,
    latent_clf_loss: Module = None,
    beta: float = 0.001,
    lamb: float = 1e-8,
    use_clf: bool = True,
    phase: str = "train",
    device: str = "cuda:0",
) -> dict:

    # Get domain configurations for the domain
    domain_model_config = domain_config.domain_model_config
    data_loader_dict = domain_config.data_loader_dict
    data_loader = data_loader_dict[phase]
    data_key = domain_config.data_key
    label_key = domain_config.label_key

    # Initialize epoch statistics
    recon_loss = 0
    clf_loss = 0
    kl_loss = 0
    total_loss = 0

    correct_preds = 0
    n_preds = 0

    # Iterate over batches
    for index, samples in enumerate(data_loader):
        # Set model_configs
        domain_model_config.inputs = samples[data_key]
        domain_model_config.labels = samples[label_key]

        batch_statistics = train_autoencoder(
            domain_model_config=domain_model_config,
            latent_clf=latent_clf,
            latent_clf_optimizer=latent_clf_optimizer,
            latent_clf_loss=latent_clf_loss,
            beta=beta,
            lamb=lamb,
            phase=phase,
            device=device,
            use_clf=use_clf
        )

        recon_loss += batch_statistics["recon_loss"]
        if use_clf:
            clf_loss += batch_statistics["clf_loss"]
            correct_preds += batch_statistics["accuracy"][0]
            n_preds += batch_statistics["accuracy"][1]
        kl_loss += batch_statistics["kl_loss"]
        total_loss += batch_statistics["total_loss"]



    # Get average over batches for statistics
    recon_loss /= len(data_loader.dataset)
    kl_loss /= len(data_loader.dataset)
    total_loss /= len(data_loader.dataset)
    if use_clf:
        accuracy = correct_preds / len(data_loader.dataset)
    else:
        accuracy = 0

    epoch_statistics = {
        "recon_loss": recon_loss,
        "kl_loss": kl_loss,
        "accuracy": accuracy,
        "total_loss": total_loss,
    }

    if use_clf:
        clf_loss /= n_preds
        epoch_statistics["clf_loss"] = clf_loss

    return epoch_statistics


def train_val_test_loop_vae(
    output_dir: str,
    domain_config: DomainConfig,
    latent_clf_config: dict = None,
    beta: float = 0.001,
    lamb: float = 0.00000001,
    use_clf: bool = False,
    num_epochs: int = 500,
    save_freq: int = 10,
    early_stopping: int = 20,
    device: str = None,
) -> Tuple[dict, dict]:
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Get available device, if cuda is available the GPU will be used
    if not device:
        device = get_device()

    # Store start time of the training
    start_time = time.time()

    # Initialize early stopping counter
    es_counter = 0
    if early_stopping < 0:
        early_stopping = num_epochs

    total_loss_dict = {"train": [], "val": []}

    # Reserve space for best model weights
    best_vae_weights = domain_config.domain_model_config.model.cpu().state_dict()

    if use_clf:
        latent_clf = latent_clf_config["model"]
        latent_clf_optimizer = latent_clf_config["optimizer"]
        latent_clf_loss = latent_clf_config["loss"]
    else:
        latent_clf = None
        latent_clf_optimizer = None
        latent_clf_loss = None

    # Reserve space for best latent clf weights
    if latent_clf is not None:
        best_clf_weights = latent_clf.cpu().state_dict()
    else:
        best_clf_weights = None

    best_model_configs = {
        "best_vae": best_vae_weights,
        "best_clf_weights": best_clf_weights,
    }

    best_total_loss = np.infty

    # Iterate over the epochs
    for i in range(num_epochs):
        logging.debug("---" * 20)
        logging.debug("---" * 20)
        logging.debug("Started epoch {}/{}".format(i + 1, num_epochs))
        logging.debug("---" * 20)

        # Check if early stopping is triggered
        if es_counter > early_stopping:
            logging.debug(
                "Training was stopped early due to no improvement of the validation"
                " loss for {} epochs.".format(early_stopping)
            )
            break

        # Iterate over training and validation phase
        for phase in ["train", "val"]:
            epoch_statistics = process_epoch_single_domain(
                domain_config=domain_config,
                latent_clf=latent_clf,
                latent_clf_optimizer=latent_clf_optimizer,
                latent_clf_loss=latent_clf_loss,
                beta=beta,
                lamb=lamb,
                use_clf=use_clf,
                phase=phase,
                device=device,
            )

            logging.debug(
                "{} LOSS STATISTICS FOR EPOCH {}: ".format(phase.upper(), i + 1)
            )

            logging.debug(
                "Reconstruction loss for {} domain: {:.8f}".format(
                    domain_config.name, epoch_statistics["recon_loss"]
                )
            )
            logging.debug(
                "KL loss for {} domain: {:.8f}".format(
                    domain_config.name, epoch_statistics["kl_loss"]
                )
            )

            if use_clf:
                logging.debug(
                    "Latent classifier loss for {} domain: {:.8f}".format(
                        domain_config.name, epoch_statistics["clf_loss"]
                    )
                )

                logging.debug(
                    "Latent classifier accuracy for {} domain: {:.8f}".format(
                        domain_config.name, epoch_statistics["accuracy"]
                    )
                )

            epoch_total_loss = epoch_statistics["total_loss"]
            total_loss_dict[phase].append(epoch_total_loss)
            logging.debug("***" * 20)
            logging.debug(
                "Total {} loss for {} domain: {:.8f}".format(
                    phase, domain_config.name, epoch_total_loss
                )
            )
            logging.debug("***" * 20)

            if phase == "val":
                # Save model states if current parameters give the best validation loss
                if epoch_total_loss < best_total_loss:
                    es_counter = 0
                    best_total_loss = epoch_total_loss

                    best_vae_weights = copy.deepcopy(
                        domain_config.domain_model_config.model.cpu().state_dict()
                    )
                    best_model_configs["best_vae_weights"] = best_vae_weights

                    torch.save(
                        best_vae_weights, "{}/best_vae.pth".format(output_dir),
                    )

                    if latent_clf is not None:
                        best_latent_clf_weights = copy.deepcopy(
                            latent_clf.cpu().state_dict()
                        )
                        best_model_configs["clf_weights"] = best_latent_clf_weights
                        torch.save(
                            best_latent_clf_weights,
                            "{}/best_clf.pth".format(output_dir),
                        )
                else:
                    es_counter += 1

                if i % save_freq == 0:

                    visualize_image_vae_performance(
                        domain_model_config=domain_config.domain_model_config,
                        epoch=i + 1,
                        output_dir=output_dir,
                        device=device,
                    )

                    # Save model states regularly
                    checkpoint_dir = "{}/epoch_{}".format(output_dir, i + 1)
                    os.makedirs(checkpoint_dir, exist_ok=True)
                    torch.save(
                        domain_config.domain_model_config.model.state_dict(),
                        "{}/vae.pth".format(checkpoint_dir),
                    )
                    if use_clf:
                        torch.save(
                        latent_clf.state_dict(),
                        "{}/latent_clf.pth".format(checkpoint_dir),
                    )

    # Training complete
    time_elapsed = time.time() - start_time

    logging.debug("###" * 20)
    logging.debug(
        "Training completed in {:.0f}m {:0f}s".format(
            time_elapsed // 60, time_elapsed % 60
        )
    )

    # Load best model
    domain_config.domain_model_config.model.load_state_dict(best_vae_weights)
    if latent_clf is not None:
        latent_clf.load_state_dict(best_clf_weights)

    if "test" in domain_config.data_loader_dict:
        epoch_statistics = process_epoch_single_domain(
            domain_config=domain_config,
            latent_clf=latent_clf,
            latent_clf_optimizer=latent_clf_optimizer,
            latent_clf_loss=latent_clf_loss,
            beta=beta,
            lamb=lamb,
            use_clf=use_clf,
            phase="test",
            device=device,
        )

        logging.debug("TEST LOSS STATISTICS")

        logging.debug(
            "Reconstruction Loss for {} domain: {:.8f}".format(
                domain_config.name, epoch_statistics["recon_loss"]
            )
        )

        if use_clf:
            logging.debug(
                "Latent classifier loss for {} domain: {:.8f}".format(
                    domain_config.name, epoch_statistics["clf_loss"]
                )
            )
        logging.debug("***" * 20)
        logging.debug(
            "Total test loss for {} domain: {:.8f}".format(
                domain_config.name, epoch_statistics["total_loss"]
            )
        )
        logging.debug("***" * 20)

    # Summarize return parameters
    fitted_models = {"vae": domain_config.domain_model_config.model}
    if latent_clf is not None:
        fitted_models["latent_clf"] = latent_clf

    return fitted_models, total_loss_dict
