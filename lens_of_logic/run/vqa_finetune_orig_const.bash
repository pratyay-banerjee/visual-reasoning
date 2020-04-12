# The name of this experiment.
name=$2

# Save logs and models under snap/vqa; make backup.
output=snap/vqa/$name
mkdir -p $output/src
cp -r src/* $output/src/
cp $0 $output/run.bash

# See Readme.md for option details.
CUDA_VISIBLE_DEVICES=$1 PYTHONPATH=$PYTHONPATH:./src \
    nohup python src/tasks/vqa.py \
    --train train --valid minival,nominival  \
    --data $3 \
    --llayers 9 --xlayers 5 --rlayers 5 \
    --load /scratch/pbanerj6/VQA_LOL/models/snap/vqa/vqa_lxr955_logic_exp1/BEST \
    --batchSize 128 --optim bert --lr 5e-5 --epochs 10 \
    --tqdm --output $output ${@:4} > outputs/$name.lol.out &
