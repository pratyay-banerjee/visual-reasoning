# coding=utf-8
# Copyleft 2019 project LXRT.

import json
import pickle

import numpy as np
import torch
from torch.utils.data import Dataset

from param import args
from utils import load_obj_tsv
from tqdm import tqdm

# Load part of the dataset for fast checking.
# Notice that here is the number of images instead of the number of data,
# which means all related data to the images would be used.
TINY_IMG_NUM = 512
FAST_IMG_NUM = 5000

# The path to data and image features.
VQA_DATA_ROOT = 'data/vqa/'
MSCOCO_IMGFEAT_ROOT = 'data/mscoco_imgfeat/'

answer_type_map = {"yes/no":[1,0,0],"number":[0,1,0],"other":[0,0,1]}

class VQADataset:
    """
    A VQA data example in json file:
        {
            "answer_type": "other",
            "img_id": "COCO_train2014_000000458752",
            "label": {
                "net": 1
            },
            "question_id": 458752000,
            "question_type": "what is this",
            "sent": "What is this photo taken looking through?"
        }
    """
    def __init__(self, splits: str,folder: str):
        self.name = splits
        self.splits = splits.split(',')

        # Loading datasets
        self.data = []
        for split in tqdm(self.splits,ascii=True,desc="Loading splits"):
            self.data.extend(json.load(open("data/vqa/%s/%s.json"%(folder,split))))
        print("Data folder:%s"%folder,flush=True)
        print("Loading from %d data from split(s) %s."%(len(self.data), self.name),flush=True)

        # Convert list to dict (for evaluation)
        self.id2datum = {
#             datum['question_id']: datum
            ix : datum
            for ix,datum in tqdm(enumerate(self.data),ascii=True,desc="Converting List to Dict")
        }

        # Answers
        self.ans2label = json.load(open("data/vqa/trainval_ans2label.json"))
        self.label2ans = json.load(open("data/vqa/trainval_label2ans.json"))
        assert len(self.ans2label) == len(self.label2ans)

    @property
    def num_answers(self):
        return len(self.ans2label)

    def __len__(self):
        return len(self.data)


"""
An example in obj36 tsv:
FIELDNAMES = ["img_id", "img_h", "img_w", "objects_id", "objects_conf",
              "attrs_id", "attrs_conf", "num_boxes", "boxes", "features"]
FIELDNAMES would be keys in the dict returned by load_obj_tsv.
"""
class VQATorchDataset(Dataset):
    def __init__(self, dataset: VQADataset):
        super().__init__()
        self.raw_dataset = dataset

        if args.tiny:
            topk = TINY_IMG_NUM
        elif args.fast:
            topk = FAST_IMG_NUM
        else:
            topk = None

        # Loading detection features to img_data
        img_data = []
        if 'train' in dataset.splits:
            img_data.extend(load_obj_tsv('data/mscoco_imgfeat/train2014_obj36.tsv', topk=topk))
        if 'valid' in dataset.splits:
            img_data.extend(load_obj_tsv('data/mscoco_imgfeat/val2014_obj36.tsv', topk=topk))
        if 'minival' in dataset.splits:
            # minival is 5K images in the intersection of MSCOCO valid and VG,
            # which is used in evaluating LXMERT pretraining performance.
            # It is saved as the top 5K features in val2014_obj36.tsv
            if topk is None:
                topk = 5000
            img_data.extend(load_obj_tsv('data/mscoco_imgfeat/val2014_obj36.tsv', topk=topk))
        if 'nominival' in dataset.splits:
            # nominival = mscoco val - minival
            img_data.extend(load_obj_tsv('data/mscoco_imgfeat/val2014_obj36.tsv', topk=topk))
        if 'test' in dataset.name:      # If dataset contains any test split
            img_data.extend(load_obj_tsv('data/mscoco_imgfeat/test2015_obj36.tsv', topk=topk))

        # Convert img list to dict
        self.imgid2img = {}
        for img_datum in img_data:
            self.imgid2img[img_datum['img_id']] = img_datum

        # Only kept the data with loaded image features
        self.data = []
        for datum in tqdm(self.raw_dataset.data,ascii=True,desc="Loading Image features"):
            if datum['img_id'] in self.imgid2img:
                self.data.append(datum)
        print("Use %d data in torch dataset" % (len(self.data)),flush=True)
        print(flush=True)
        
        self.atleast1=False
        
        self.opmap={
            "notQ1":1,
            "notQ1_and_notQ2":10,
            "notQ1_and_Q2":9,
            "notQ1_or_notQ2":8,
            "notQ1_or_Q2":7,
            "notQ_and_notQ2":10,
            "notQ_or_notQ2":8,
            "notQ2":2,
            "Q1":0,
            "Q1_and_notQ2":6,
            "Q1_and_Q2":4,
            "Q1_or_notQ2":5,
            "Q1_or_Q2":3,
            "Q2":0,
        }

    def __len__(self):
        return len(self.data)

    def __getitem__(self, item: int):
        datum = self.data[item]

        img_id = datum['img_id']
        ques_id = item
        ques = datum['sent']
        
        q1,q2 = ques,ques
        if "q1" in datum:
            q1 = datum["q1"]
            q2 = datum["q2"]
        
        op=0
        if "op" in datum:
            op = self.opmap[datum["op"]]
        
        # Get image info
        img_info = self.imgid2img[img_id]
        obj_num = img_info['num_boxes']
        feats = img_info['features'].copy()
        boxes = img_info['boxes'].copy()
        assert obj_num == len(boxes) == len(feats)

        # Normalize the boxes (to 0 ~ 1)
        img_h, img_w = img_info['img_h'], img_info['img_w']
        boxes = boxes.copy()
        boxes[:, (0, 2)] /= img_w
        boxes[:, (1, 3)] /= img_h
        np.testing.assert_array_less(boxes, 1+1e-5)
        np.testing.assert_array_less(-boxes, 0+1e-5)

        # Provide label (target)
        if 'label' in datum:
            label = datum['label']
            q1_label,q2_label = label,label
            if "q1_label" in datum:
                q1_label = datum['q1_label']
                q2_label = datum['q2_label']
            
            target = torch.zeros(self.raw_dataset.num_answers)
            q1_target = torch.zeros(self.raw_dataset.num_answers)
            q2_target = torch.zeros(self.raw_dataset.num_answers)
            
            
            yesnotypetargets = torch.zeros(4)
            q1yntypetargets = torch.zeros(4)
            q2yntypetargets = torch.zeros(4)
            
            q1ynfeats = [0,0,0,1]
            q2ynfeats = [0,0,0,1]
            
            answertypefeats = answer_type_map[datum['answer_type']]
            if "feature" in datum:
                yesnotypefeats = datum["feature"]
                if yesnotypefeats[-1]!= 1:
                    if not self.atleast1:
                        assert (yesnotypefeats[0]==1) or (yesnotypefeats[1]==1) or (yesnotypefeats[2] ==1) 
#                         print(datum)
                        self.atleast1=True
            else:
                yesnotypefeats = [0,0,0,1]

            for ans, score in label.items():
                target[self.raw_dataset.ans2label[ans]] = score
                
            for ans, score in q1_label.items():
                q1_target[self.raw_dataset.ans2label[ans]] = score
                
            for ans, score in q2_label.items():
                q2_target[self.raw_dataset.ans2label[ans]] = score    
            
            
            typetarget = 0
            for ix,score in enumerate(answertypefeats):
                if score==1:
                    typetarget=ix
                
            for ix,score in enumerate(yesnotypefeats):
                yesnotypetargets[ix] = score
            for ix,score in enumerate(q1ynfeats):
                q1yntypetargets[ix] = score
            for ix,score in enumerate(q2ynfeats):
                q2yntypetargets[ix] = score
            
            return ques_id, feats, boxes, ques, op, q1, q2, typetarget, 0, 0, yesnotypetargets, q1yntypetargets, q2yntypetargets, target, q1_target, q2_target
        else:
            return ques_id, feats, boxes, ques, 0,  "", "", 0,  0, 0, torch.zeros(4), torch.zeros(4),torch.zeros(4), torch.zeros(3129), torch.zeros(3129), torch.zeros(3129)


class VQAEvaluator:
    def __init__(self, dataset: VQADataset):
        self.dataset = dataset

    def evaluate(self, quesid2ans: dict):
        score = 0.
        for quesid, ans in quesid2ans.items():
            datum = self.dataset.id2datum[quesid]
            label = datum['label']
            if ans in label:
                score += label[ans]
        return score / len(quesid2ans)

    def dump_result(self, quesid2ans: dict, path):
        """
        Dump results to a json file, which could be submitted to the VQA online evaluation.
        VQA json file submission requirement:
            results = [result]
            result = {
                "question_id": int,
                "answer": str
            }

        :param quesid2ans: dict of quesid --> ans
        :param path: The desired path of saved file.
        """
        with open(path, 'w') as f:
            result = []
            for ques_id, ans in quesid2ans.items():
                result.append({
                    'question_id': ques_id,
                    'answer': ans
                })
            json.dump(result, f, indent=4, sort_keys=True)


