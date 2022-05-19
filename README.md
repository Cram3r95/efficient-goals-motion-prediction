# Exploring Map-based Features for Efficient Attention-based Vehicle Vehicle Motion Prediction

conda create --name efficient-goals-motion-prediction python=3.8 \
conda install -n carlos_efficient-goals-motion-prediction ipykernel --update-deps --force-reinstall

python3 -m pip install --upgrade pip \
python3 -m pip install --upgrade Pillow \

pip install \
    prodict \
    torch \
    pyyaml \
    torchvision \
    tensorboard

Download argoverse-api (1.0) in another folder (out of this directory). \
Go to the argoverse-api folder: 
```
    pip install -e . (N.B. You must have the conda environment activated in order to have argoverse as a Python package of your environment)
```