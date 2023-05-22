import os

import pandas as pd
import torch
import torch_em.self_training as self_training

import common


def check_loader(args, n_images=5):
    from torch_em.util.debug import check_loader

    cell_types = args.cell_types
    print("The cell types", cell_types, "were selected.")
    print("Checking the unsupervised loader for the first cell type", cell_types[0])

    loader = common.get_unsupervised_loader(
        args, "train", cell_types[0],
        teacher_augmentation="weak", student_augmentation="strong",
    )
    check_loader(loader, n_images)


def _train_source_target(args, source_cell_type, target_cell_type):
    model = common.get_punet()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-5)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.9, patience=10)

    # self training functionality
    # - when thresh is None, we don't mask the reconstruction loss (RL) with label filters
    # - when thresh is passed (float), we mask the RL with weighted consensus label filters
    # - when thresh is passed (float) with args.consensus_masking, we mask the RL with masked consensus label filters
    thresh = args.confidence_threshold
    if thresh is None:
        assert args.consensus_masking is False, "Provide a confidence threshold to use consensus masking"

    pseudo_labeler = self_training.ProbabilisticPseudoLabeler(activation=torch.nn.Sigmoid(),
                                                              confidence_threshold=thresh, prior_samples=16,
                                                              consensus_masking=args.consensus_masking)
    loss = self_training.ProbabilisticUNetLoss()
    loss_and_metric = self_training.ProbabilisticUNetLossAndMetric()

    # data loaders
    supervised_train_loader = common.get_supervised_loader(args, "train", source_cell_type, args.batch_size)
    supervised_val_loader = common.get_supervised_loader(args, "val", source_cell_type, 1)
    unsupervised_train_loader = common.get_unsupervised_loader(
        args, args.batch_size, "train", target_cell_type,
        teacher_augmentation="weak", student_augmentation="strong-joint",
    )
    unsupervised_val_loader = common.get_unsupervised_loader(
        args, 1, "val", target_cell_type,
        teacher_augmentation="weak", student_augmentation="strong-joint",
    )

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    if args.consensus_masking:
        name = f"punet_adamatch/thresh-{thresh}-masking"
    else:
        name = f"punet_adamatch/thresh-{thresh}"

    if args.distribution_alignment:
        assert args.output is not None
        print(f"Getting scores for Source {source_cell_type} at Targets {target_cell_type}")
        pred_folder = args.output + f"punet_source/{source_cell_type}/{target_cell_type}/"
        src_dist = common.compute_class_distribution(pred_folder)
        name = f"{name}-distro-align"
    else:
        src_dist = None

    name = name + f"/{source_cell_type}/{target_cell_type}"

    trainer = self_training.FixMatchTrainer(
        name=name,
        model=model,
        optimizer=optimizer,
        lr_scheduler=scheduler,
        pseudo_labeler=pseudo_labeler,
        unsupervised_loss=loss,
        unsupervised_loss_and_metric=loss_and_metric,
        supervised_train_loader=supervised_train_loader,
        unsupervised_train_loader=unsupervised_train_loader,
        supervised_val_loader=supervised_val_loader,
        unsupervised_val_loader=unsupervised_val_loader,
        supervised_loss=loss,
        supervised_loss_and_metric=loss_and_metric,
        logger=None,
        mixed_precision=True,
        device=device,
        log_image_interval=100,
        save_root=args.save_root,
        source_distribution=src_dist,
        compile_model=False
    )
    trainer.fit(args.n_iterations)


def _train_source(args, cell_type):
    if args.target_ct is None:
        target_cell_list = common.CELL_TYPES
    else:
        target_cell_list = args.target_ct

    for target_cell_type in target_cell_list:
        print("Training on target cell type:", target_cell_type)
        if target_cell_type == cell_type:
            continue
        _train_source_target(args, cell_type, target_cell_type)


def run_training(args):
    for cell_type in args.cell_types:
        print("Start training for source cell type:", cell_type)
        _train_source(args, cell_type)


def run_evaluation(args):
    results = []
    for ct in args.cell_types:
        res = common.evaluate_transfered_model(args, ct, "punet_adamatch",
                                               get_model=common.get_punet,
                                               prediction_function=common.get_punet_predictions)
        results.append(res)
    results = pd.concat(results)
    print("Evaluation results:")
    print(results)
    result_folder = "./results"
    os.makedirs(result_folder, exist_ok=True)
    results.to_csv(os.path.join(result_folder, "punet_adamatch.csv"), index=False)


def main():
    parser = common.get_parser(default_iterations=100000, default_batch_size=4)
    parser.add_argument("--confidence_threshold", default=None, type=float)
    parser.add_argument("--consensus_masking", action='store_true')
    args = parser.parse_args()
    if args.phase in ("c", "check"):
        check_loader(args)
    elif args.phase in ("t", "train"):
        run_training(args)
    elif args.phase in ("e", "evaluate"):
        run_evaluation(args)
    else:
        raise ValueError(f"Got phase={args.phase}, expect one of check, train, evaluate.")


if __name__ == "__main__":
    main()
