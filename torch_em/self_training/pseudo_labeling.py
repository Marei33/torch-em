import torch


class DefaultPseudoLabeler:
    """Compute pseudo labels.

    Parameters:
        activation [nn.Module, callable] - activation function applied to the teacher prediction.
        confidence_threshold [float] - threshold for computing a mask for filterign the pseudo labels.
            If none is given no mask will be computed (default: None)
        threshold_from_both_sides [bool] - whether to include both values bigger than the threshold
            and smaller than 1 - it, or only values bigger than it in the mask.
            The former should be used for binary labels, the latter for for multiclass labels (default: False)
    """
    def __init__(self, activation=None, confidence_threshold=None, threshold_from_both_sides=True):
        self.activation = activation
        self.confidence_threshold = confidence_threshold
        self.threshold_from_both_sides = threshold_from_both_sides
        # TODO serialize the class names and kwargs for activation instead
        self.init_kwargs = {
            "activation": None, "confidence_threshold": confidence_threshold,
            "threshold_from_both_sides": threshold_from_both_sides
        }

    def _compute_label_mask_both_sides(self, pseudo_labels):
        upper_threshold = self.confidence_threshold
        lower_threshold = 1.0 - self.confidence_threshold
        mask = ((pseudo_labels >= upper_threshold) + (pseudo_labels <= lower_threshold)).to(dtype=torch.float32)
        return mask

    def _compute_label_mask_one_side(self, pseudo_labels):
        mask = (pseudo_labels >= self.confidence_threshold)
        return mask

    def __call__(self, teacher, input_):
        pseudo_labels = teacher(input_)
        if self.activation is not None:
            pseudo_labels = self.activation(pseudo_labels)
        if self.confidence_threshold is None:
            label_mask = None
        else:
            label_mask = self._compute_label_mask_both_sides(pseudo_labels) if self.threshold_from_both_sides\
                else self._compute_label_mask_one_side(pseudo_labels)
        return pseudo_labels, label_mask
    
    def step(self, epoch):
        pass


class ProbabilisticPseudoLabeler:
    """Compute pseudo labels from the Probabilistic UNet.

    Parameters:
        activation [nn.Module, callable] - activation function applied to the teacher prediction.
        confidence_threshold [float] - threshold for computing a mask for filterign the pseudo labels.
            If none is given no mask will be computed (default: None)
        threshold_from_both_sides [bool] - whether to include both values bigger than the threshold
            and smaller than 1 - it, or only values bigger than it in the mask.
            The former should be used for binary labels, the latter for for multiclass labels (default: False)
        prior_samples [int] - the number of times we want to sample from the
            prior distribution per inputs (default: 16)
        consensus_masking [bool] - whether to activate consensus masking in the label filter (default: False)
            If false, the weighted consensus response (weighted per-pixel response) is returned
            If true, the masked consensus response (complete aggrement of pixels) is returned
    """
    def __init__(self, activation=None, confidence_threshold=None, threshold_from_both_sides=True,
                 prior_samples=16, consensus_masking=False):
        self.activation = activation
        self.confidence_threshold = confidence_threshold
        self.threshold_from_both_sides = threshold_from_both_sides
        self.prior_samples = prior_samples
        self.consensus_masking = consensus_masking
        # TODO serialize the class names and kwargs for activation instead
        self.init_kwargs = {
            "activation": None, "confidence_threshold": confidence_threshold,
            "threshold_from_both_sides": threshold_from_both_sides
        }

    def _compute_label_mask_both_sides(self, pseudo_labels):
        upper_threshold = self.confidence_threshold
        lower_threshold = 1.0 - self.confidence_threshold
        mask = [torch.where((sample >= upper_threshold) + (sample <= lower_threshold),
                            torch.tensor(1.),
                            torch.tensor(0.)) for sample in pseudo_labels]
        return mask

    def _compute_label_mask_one_side(self, pseudo_labels):
        mask = [torch.where((sample >= self.confidence_threshold),
                            torch.tensor(1.),
                            torch.tensor(0.)) for sample in pseudo_labels]
        return mask

    def __call__(self, teacher, input_):
        teacher.forward(input_)
        if self.activation is not None:
            pseudo_labels = [self.activation(teacher.sample()) for _ in range(self.prior_samples)]
        else:
            pseudo_labels = [teacher.sample() for _ in range(self.prior_samples)]
        pseudo_labels = torch.stack(pseudo_labels, dim=0).sum(dim=0)/self.prior_samples

        if self.confidence_threshold is None:
            label_mask = None
        else:
            label_mask = self._compute_label_mask_both_sides(pseudo_labels) if self.threshold_from_both_sides \
                else self._compute_label_mask_one_side(pseudo_labels)
            label_mask = torch.stack(label_mask, dim=0).sum(dim=0)/self.prior_samples
            if self.consensus_masking:
                label_mask = torch.where(label_mask == 1, 1, 0)

        return pseudo_labels, label_mask
    
    def step(self, epoch):
        pass


class ScheduledPseudoLabeler:
    """Compute pseudo labels based on varying confidence thresholds.

    Parameters:
        activation [nn.Module, callable] - activation function applied to the teacher prediction.
        confidence_threshold [float] - threshold for computing a mask for filterign the pseudo labels.
            If none is given no mask will be computed (default: None)
        threshold_from_both_sides [bool] - whether to include both values bigger than the threshold
            and smaller than 1 - it, or only values bigger than it in the mask.
            The former should be used for binary labels, the latter for for multiclass labels (default: False)
    """
    def __init__(
        self,
        activation=None,
        confidence_threshold=None,
        threshold_from_both_sides=True,
        mode="min",
        factor=0.05,
        patience=10,
        threshold=1e-4,
        threshold_mode="abs",
        min_ct=0.5,
        eps=1e-8,
        verbose=True,
    ):
        self.activation = activation
        self.confidence_threshold = confidence_threshold
        self.threshold_from_both_sides = threshold_from_both_sides
        self.init_kwargs = {
            "activation": None, "confidence_threshold": confidence_threshold,
            "threshold_from_both_sides": threshold_from_both_sides
        }
        # scheduler arguments
        if mode not in {'min', 'max'}:
            raise ValueError(f"Invalid mode: {mode}. Mode should be 'min' or 'max'.")
        self.mode = mode

        if factor >= 1.0:
            raise ValueError("Factor should be < 1.0.")
        self.factor = factor

        self.patience = patience
        self.threshold = threshold

        if threshold_mode not in {'rel', 'abs'}:
            raise ValueError(f"Invalid threshold mode: {mode}. Threshold mode should be 'rel' or 'abs'.")
        self.threshold_mode = threshold_mode

        self.min_ct = min_ct
        self.eps = eps
        self.verbose = verbose

        if mode == "min":
            self.best = float('inf')
        else: # mode == 'max':
            self.best = float('-inf')
            
        # self.best = 0
        self.num_bad_epochs: int = 0
        self.last_epoch = 0
        

    def _compute_label_mask_both_sides(self, pseudo_labels):
        upper_threshold = self.confidence_threshold
        lower_threshold = 1.0 - self.confidence_threshold
        mask = ((pseudo_labels >= upper_threshold) + (pseudo_labels <= lower_threshold)).to(dtype=torch.float32)
        return mask

    def _compute_label_mask_one_side(self, pseudo_labels):
        mask = (pseudo_labels >= self.confidence_threshold)
        return mask

    def __call__(self, teacher, input_):
        pseudo_labels = teacher(input_)
        if self.activation is not None:
            pseudo_labels = self.activation(pseudo_labels)
        if self.confidence_threshold is None:
            label_mask = None
        else:
            label_mask = self._compute_label_mask_both_sides(pseudo_labels) if self.threshold_from_both_sides\
                else self._compute_label_mask_one_side(pseudo_labels)
        return pseudo_labels, label_mask
    

    def _is_better(self, a, best):
        if self.mode == "min" and self.threshold_mode == "rel":
            rel_epsilon = 1.0 - self.threshold
            return a < best * rel_epsilon

        elif self.mode == "min" and self.threshold_mode == "abs":
            return a < best - self.threshold

        elif self.mode == "max" and self.threshold_mode == "rel":
            rel_epsilon = self.threshold + 1.0
            return a > best * rel_epsilon

        else:  # mode == 'max' and epsilon_mode == 'abs':
            return a > best + self.threshold
    

    def _reduce_ct(self, epoch):
        old_ct = self.confidence_threshold
        if self.threshold_mode == "rel":
            new_ct = max(self.confidence_threshold * self.factor, self.min_ct)
        else: # threshold_mode == 'abs':
            new_ct = max(self.confidence_threshold - self.factor, self.min_ct)
        if old_ct - new_ct > self.eps:
                self.confidence_threshold = new_ct
        if self.verbose:
            print(f"Epoch {epoch}: reducing confidence threshold from {old_ct} to {self.confidence_threshold}")


    def step(self, metric, epoch=None):
        current = float(metric)
        if epoch is None:
            epoch = self.last_epoch + 1
        self.last_epoch = epoch
        print(f"Current metric in epoch {epoch}: {current}")

        if self._is_better(current, self.best):
            self.best = current
            self.num_bad_epochs = 0
        else:
            self.num_bad_epochs += 1

        if self.num_bad_epochs > self.patience:
            self._reduce_ct(epoch)
            self.num_bad_epochs = 0
