GPUID=$1
OUTDIR=outputs/split_MNIST_interval_ic_per_weight
REPEAT=10
mkdir -p $OUTDIR

python -u intervalBatchLearn.py --gpuid $GPUID --repeat $REPEAT --incremental_class \
       --optimizer Adam --force_out_dim 10 --no_class_remap --first_split_size 2 \
       --other_split_size 2 --model_name interval_mlp400 --agent_type interval --agent_name IntervalNet \
       --schedule 12 --batch_size 100 --lr 0.001 --eps_per_model --kappa_min 0 \
       --kappa_epoch 1 --eps_epoch 12 --eps_val 6 3 1 1 0.5 --eps_max 0 --clipping \
       | tee ${OUTDIR}/experimental.log