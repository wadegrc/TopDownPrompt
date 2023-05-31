# bash experiments/cifar-100.sh
# experiment settings
DATASET=imagenet-r
N_CLASS=200

# save directory
OUTDIR=./outputs/${DATASET}/10-task

# hard coded inputs
GPUID='0 1 2 3'
CONFIG=./configs/imagenet-r_prompt.yaml
REPEAT=1
OVERWRITE=0

###############################################################

# process inputs
mkdir -p $OUTDIR

# CODA-P
#
# prompt parameter args:
#    arg 1 = prompt component pool size
#    arg 2 = prompt length
#    arg 3 = ortho penalty loss weight
python -u run.py --config $CONFIG --gpuid $GPUID --repeat $REPEAT --overwrite $OVERWRITE \
    --learner_type prompt --learner_name TopDownPrompt \
    --prompt_param 100 8 0.1 \
    --log_dir ${OUTDIR}/coda-p

# DualPrompt
#
# prompt parameter args:
#    arg 1 = e-prompt pool size (# tasks)
#    arg 2 = e-prompt pool length
#    arg 3 = g-prompt pool length
