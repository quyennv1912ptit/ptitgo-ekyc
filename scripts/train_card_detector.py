"""Train a four-corner card pose model on local CPU or Kaggle GPU."""

import argparse
import json
import os
from pathlib import Path

from select_card_checkpoint import select_checkpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--device", default="cpu", help="cpu locally; 0 for the first Kaggle GPU")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--project", type=Path, default=Path("models/card_pose_training"))
    parser.add_argument("--weights", type=Path, default=Path("models/pretrained/yolov8n-pose.pt"))
    parser.add_argument("--freeze", type=int, default=0)
    parser.add_argument("--patience", type=int, default=30)
    parser.add_argument("--pose-weight", type=float, default=36)
    parser.add_argument("--degrees", type=float, default=25)
    parser.add_argument("--lr0", type=float, default=0.001)
    parser.add_argument(
        "--output", type=Path, default=Path("models/card_yolov8n_pose.experimental.pt")
    )
    args = parser.parse_args()
    os.environ["YOLO_CONFIG_DIR"] = str(Path("models/ultralytics_settings").resolve())
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_OFFLINE"] = "true"
    import torch
    from ultralytics import YOLO, settings

    settings.update(
        dict.fromkeys(
            ["sync", "clearml", "comet", "dvc", "mlflow", "raytune", "tensorboard", "wandb"], False
        )
    )

    torch.set_num_threads(args.threads)
    pretrained = args.weights
    pretrained.parent.mkdir(parents=True, exist_ok=True)
    model = YOLO(str(pretrained))
    # select_device("cpu") resets Torch threads before callbacks are invoked.
    model.add_callback("on_train_start", lambda trainer: torch.set_num_threads(args.threads))
    model.train(
        data=str(args.data.resolve()),
        epochs=args.epochs,
        batch=args.batch,
        imgsz=args.imgsz,
        device=args.device,
        workers=args.workers,
        optimizer="AdamW",
        lr0=args.lr0,
        freeze=args.freeze,
        patience=args.patience,
        pose=args.pose_weight,
        seed=42,
        deterministic=True,
        mosaic=0,
        mixup=0,
        fliplr=0,
        flipud=0,
        degrees=args.degrees,
        scale=0.2,
        translate=0.1,
        project=str(args.project.resolve()),
        name="baseline",
        exist_ok=False,
        plots=False,
        verbose=False,
    )
    best = Path(model.trainer.best)
    if not best.is_file():
        raise RuntimeError("No trained checkpoint produced")
    selection = select_checkpoint(args.data, [best, Path(model.trainer.last)], args.output)
    verified = YOLO(str(args.output))
    if verified.names != {0: "card"} or verified.model.yaml.get("kpt_shape") != [4, 3]:
        raise RuntimeError("Unexpected checkpoint metadata")
    report = {
        "data": str(args.data.resolve()),
        "checkpoint": str(args.output.resolve()),
        "epochs_requested": args.epochs,
        "initial_weights": str(args.weights),
        "freeze": args.freeze,
        "pose_weight": args.pose_weight,
        "degrees": args.degrees,
        "lr0": args.lr0,
        "device": args.device,
        "workers": args.workers,
        "best": str(best),
        "ultralytics_best_metrics": model.trainer.metrics,
        "corner_selection": selection,
        "experimental": True,
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(f"Experimental checkpoint: {args.output}")


if __name__ == "__main__":
    main()
