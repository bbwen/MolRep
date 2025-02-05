

import os
import random
import sklearn
import collections

from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D

from MolRep.Models.losses import get_loss_func
from MolRep.Explainer.explainerNetWrapper import ExplainerNetWrapper
from MolRep.Models.schedulers import build_lr_scheduler

from MolRep.Utils.config_from_dict import Config
from MolRep.Explainer.Metrics import attribution_metric as att_metrics
from MolRep.Utils.utils import *


GREEN_COL = (0, 1, 0)
RED_COL = (1, 0, 0)


class ExplainerExperiments:

    def __init__(self, model_configuration, dataset_config, exp_path):
        self.model_config = Config.from_dict(model_configuration) if isinstance(model_configuration, dict) else model_configuration
        self.dataset_config = dataset_config
        self.exp_path = exp_path

        if not os.path.exists(exp_path):
            os.makedirs(exp_path)


    def run_valid(self, dataset, attribution, logger, other=None):
        """
        This function returns the training and test accuracy. DO NOT USE THE TEST FOR TRAINING OR EARLY STOPPING!
        :return: (training accuracy, test accuracy)
        """
        shuffle = self.model_config['shuffle'] if 'shuffle' in self.model_config else True

        model_class = self.model_config.model
        optim_class = self.model_config.optimizer
        stopper_class = self.model_config.early_stopper
        clipping = self.model_config.gradient_clipping

        loss_fn = get_loss_func(self.dataset_config['task_type'], self.model_config.exp_name)
        shuffle = self.model_config['shuffle'] if 'shuffle' in self.model_config else True


        train_loader, scaler = dataset.get_train_loader(self.model_config['batch_size'],
                                                        shuffle=shuffle)

        model = model_class(dim_features=dataset.dim_features, dim_target=dataset.dim_target, model_configs=self.model_config, dataset_configs=self.dataset_config)
        net = ExplainerNetWrapper(model, attribution, dataset_configs=self.dataset_config, model_config=self.model_config,
                                  loss_function=loss_fn)
        optimizer = optim_class(model.parameters(),
                                lr=self.model_config['learning_rate'], weight_decay=self.model_config['l2'])
        scheduler = build_lr_scheduler(optimizer, model_configs=self.model_config, num_samples=dataset.num_samples)

        train_loss, train_metric, _, _, _, _, _ = net.train(train_loader=train_loader,
                                                            optimizer=optimizer, scheduler=scheduler,
                                                            clipping=clipping, scaler=scaler,
                                                            early_stopping=stopper_class,
                                                            logger=logger)

        if other is not None and 'model_path' in other.keys():
            save_checkpoint(path=other['model_path'], model=model, scaler=scaler)

        return train_metric

    def molecule_importance(self, dataset, attribution, logger, testing=True, other=None):

        model_class = self.model_config.model
        loss_fn = get_loss_func(self.dataset_config['task_type'], self.model_config.exp_name)
        model = model_class(dim_features=dataset.dim_features, dim_target=dataset.dim_target, model_configs=self.model_config, dataset_configs=self.dataset_config)

        assert 'model_path' in other.keys()
        model = load_checkpoint(path=other['model_path'], model=model)
        scaler, features_scaler = load_scalers(path=other['model_path'])
        net = ExplainerNetWrapper(model, attribution, dataset_configs=self.dataset_config, model_config=self.model_config,
                                  loss_function=loss_fn)

        if testing:
            test_loader = dataset.get_test_loader()
        else:
            test_loader = dataset.get_all_dataloader()
        y_preds, y_labels, results, atom_importance, bond_importance = net.explainer(test_loader=test_loader, scaler=scaler, logger=logger)

        return results, atom_importance, bond_importance

    def visualization(self, dataset, atom_importance, bond_importance, threshold=1e-4, set_weights=True, svg_dir=None, vis_factor=1.0, img_width=400, img_height=200, testing=True):

        smiles_list = dataset.get_smiles_list(testing=testing)
        att_probs = self.preprocessing_attributions(smiles_list, atom_importance, bond_importance, normalizer='MinMaxScaler')
        for idx, smiles in enumerate(smiles_list):
            mol = Chem.MolFromSmiles(smiles)
            cp = Chem.Mol(mol)
            atom_imp = att_probs[idx]

            highlightAtomColors, cp = self.determine_atom_col(cp, atom_imp, eps=0.1, set_weights=True)
            highlightAtoms = list(highlightAtomColors.keys())

            highlightBondColors = self.determine_bond_col(highlightAtomColors, mol)
            highlightBonds = list(highlightBondColors.keys())

            highlightAtomRadii = {
                # k: np.abs(v) * vis_factor for k, v in enumerate(atom_imp)
                k: 0.1 * vis_factor for k, v in enumerate(atom_imp)
            }

            rdDepictor.Compute2DCoords(cp, canonOrient=True)
            drawer = rdMolDraw2D.MolDraw2DCairo(img_width, img_height)
            drawer.DrawMolecule(
                cp,
                highlightAtoms=highlightAtoms,
                highlightAtomColors=highlightAtomColors,
                highlightAtomRadii=highlightAtomRadii,
                highlightBonds=highlightBonds,
                highlightBondColors=highlightBondColors,
            )
            drawer.FinishDrawing()
            drawer.WriteDrawingText(os.path.join(svg_dir, f"{idx}.png"))
        #     svg = drawer.GetDrawingText().replace("svg:", "")
        #     svg = None
        #     svg_list.append(svg)

        # return svg_list
        return 

    def preprocessing_attributions(self, smiles_list, atom_importance, bond_importance, normalizer='MinMaxScaler'):
        att_probs = []
        for idx, smiles in enumerate(smiles_list):
            mol = Chem.MolFromSmiles(smiles)
            atom_imp = atom_importance[idx]

            if bond_importance is not None:
                bond_imp = bond_importance[idx]

                bond_idx = []
                for bond in mol.GetBonds():
                    bond_idx.append((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))

                for (atom_i_idx, atom_j_idx), b_imp in zip(bond_idx, bond_imp):
                    atom_imp[atom_i_idx] += b_imp / 2
                    atom_imp[atom_j_idx] += b_imp / 2

            att_probs.append(atom_imp)
        
        att_probs = [att[:, -1] if att_probs[0].ndim > 1 else att for att in att_probs]
        
        att_probs = self.normalize_attributions(att_probs, normalizer)
        return att_probs

    def determine_atom_col(self, cp, atom_importance, eps=1e-5, set_weights=True):
        """ Colors atoms with positive and negative contributions
        as green and red respectively, using an `eps` absolute
        threshold.

        Parameters
        ----------
        mol : rdkit mol
        atom_importance : np.ndarray
            importances given to each atom
        bond_importance : np.ndarray
            importances given to each bond
        version : int, optional
            1. does not consider bond importance
            2. bond importance is taken into account, but fixed
            3. bond importance is treated the same as atom importance, by default 2
        eps : float, optional
            threshold value for visualization - absolute importances below `eps`
            will not be colored, by default 1e-5

        Returns
        -------
        dict
            atom indexes with their assigned color
        """
        atom_col = {}

        for idx, v in enumerate(atom_importance):
            if v > eps:
                atom_col[idx] = RED_COL
            if v < -eps:
                atom_col[idx] = RED_COL
                if set_weights:
                    cp.GetAtomWithIdx(idx).SetProp("atomNote","%.3f"%(v))
        return atom_col, cp

    def determine_bond_col(self, atom_col, mol):
        """Colors bonds depending on whether the atoms involved
        share the same color.

        Parameters
        ----------
        atom_col : np.ndarray
            coloring assigned to each atom index
        mol : rdkit mol

        Returns
        -------
        dict
            bond indexes with assigned color
        """
        bond_col = {}

        for idx, bond in enumerate(mol.GetBonds()):
            atom_i_idx, atom_j_idx = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
            if atom_i_idx in atom_col and atom_j_idx in atom_col:
                if atom_col[atom_i_idx] == atom_col[atom_j_idx]:
                    bond_col[idx] = atom_col[atom_i_idx]
        return bond_col

    def evaluate_attributions(self, dataset, atom_importance, bond_importance, binary=False):
        att_true = dataset.get_attribution_truth()

        stats = collections.OrderedDict()
        smiles_list = dataset.get_smiles_list()
        att_probs = self.preprocessing_attributions(smiles_list, atom_importance, bond_importance)
        
        if binary:
            opt_threshold = -1
            stats['ATT F1'] = np.nanmean(
                att_metrics.attribution_f1(att_true, att_probs))
            stats['ATT ACC'] = np.nanmean(
                att_metrics.attribution_accuracy(att_true, att_probs))
        else:
            opt_threshold = att_metrics.get_optimal_threshold(att_true, att_probs)
            att_binary = [np.array([1 if att>opt_threshold else 0 for att in att_prob]) for att_prob in att_probs]

            stats['ATT AUROC'] = np.nanmean(
                att_metrics.attribution_auroc(att_true, att_probs))
            stats['ATT F1'] = np.nanmean(
                att_metrics.attribution_f1(att_true, att_binary))
            stats['ATT ACC'] = np.nanmean(
                att_metrics.attribution_accuracy(att_true, att_binary))

        return stats, opt_threshold

    def evaluate_cliffs(self, dataset, atom_importance, bond_importance):
        smiles_list = dataset.get_smiles_list()
        att_true_pair = dataset.get_attribution_truth()
        att_probs = self.preprocessing_attributions(smiles_list, atom_importance, bond_importance)
        
        att_probs_reset, att_true = [], []
        smiles_list = list(smiles_list)
        for idx in range(len(att_true_pair)):
            smiles_1 = att_true_pair[idx]['SMILES_1']
            smiles_2 = att_true_pair[idx]['SMILES_2']

            idx_1 = smiles_list.index(smiles_1)
            idx_2 = smiles_list.index(smiles_2)

            att_probs_reset.append(att_probs[idx_1])
            att_true_1 = att_true_pair[idx]['attribution_1']
            att_true.append(att_true_1)

            att_probs_reset.append(att_probs[idx_2])
            att_true_2 = att_true_pair[idx]['attribution_2']
            att_true.append(att_true_2)

        opt_threshold = att_metrics.get_optimal_threshold(att_true, att_probs_reset, multi=True)
        att_binary = [np.array([1 if att>0.5 else -1 if att<(-0.5) else 0 for att in att_prob]) for att_prob in att_probs_reset]

        stats = collections.OrderedDict()
        stats['ATT F1'] = np.nanmean(
            att_metrics.attribution_f1(att_true, att_binary))
        stats['ATT ACC'] = np.nanmean(
            att_metrics.attribution_accuracy(att_true, att_binary))
        return stats, opt_threshold

    def normalize_attributions(self, att_list, positive = False, normalizer='MinMaxScaler'):
        """Normalize all nodes to 0 to 1 range via quantiles."""
        all_values = np.concatenate(att_list)
        all_values = all_values[all_values > 0] if positive else all_values

        if normalizer == 'QuantileTransformer':
            normalizer = sklearn.preprocessing.QuantileTransformer()
        else:
            normalizer = sklearn.preprocessing.MinMaxScaler()
        normalizer.fit(all_values.reshape(-1, 1))
        new_att = []
        for att in att_list:
            normed_nodes = normalizer.transform(att.reshape(-1, 1)).ravel()
            new_att.append(normed_nodes)
        return new_att
