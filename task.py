import torch
import numpy as np
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.utils import shuffle
import sys
import os
import argparse
from pathlib import Path
from tqdm import trange
import random

from TLSTM.tlstm import TLSTMConfig, TLSTM
from utils import pkl_save, pkl_load, SeqEHRLogger


def _eval(model, features, times, labels):
    model.eval()
    y_preds, y_trues, gs_labels, pred_labels = None, None, None, None
    check_inputs(features, times, labels)

    data_idxs = list(range(len(features)))
    for data_idx in data_idxs:
        # prepare data
        feature = features[data_idx]
        time = times[data_idx]
        label = labels[data_idx]
        feature_tensor = torch.tensor(feature, dtype=torch.float32).to(args.device)
        time_tensor = torch.tensor(time, dtype=torch.float32).to(args.device)
        label_tensor = torch.tensor(label, dtype=torch.float32).to(args.device)

        with torch.no_grad():
            _, logits, y_pred = model(feature_tensor, time_tensor, label_tensor)
            logits = torch.nn.functional.softmax(logits).detach().cpu().numpy()
            y_pred = y_pred.detach().cpu().numpy()
            if y_preds is None:
                pred_labels = logits
                y_preds = y_pred
                gs_labels = label
                y_trues = np.argmax(label, axis=1)
            else:
                pred_labels = np.concatenate([pred_labels, logits], axis=0)
                y_preds = np.concatenate([y_preds, y_pred], axis=0)
                gs_labels = np.concatenate([gs_labels, label], axis=0)
                y_trues = np.concatenate([y_trues, np.argmax(label, axis=1)], axis=0)
        return y_trues, y_preds, gs_labels, pred_labels


def check_inputs(*inputs):
    llen = []
    for each in inputs:
        llen.append(len(each))
    assert len(set(llen)) == 1, \
        """input datas must have same amount of data point but 
        get dims as {}
        """.format(llen)


def train(args, model, features, times, labels):
    check_inputs(features, times, labels)
    data_idxs = list(range(len(features)))
    # optimizer set up
    # # use adam to follow the original implementation
    # optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    # # using AdamW for better generalizability
    # no_decay = {'', '', '', ''}
    # optimizer_grouped_parameters = [
    #     {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
    #      'weight_decay': args.weight_decay},
    #     {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    # ]
    # optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.eps)

    # using fp16 for training rely on Nvidia apex package
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # training loop
    tr_loss = .0
    epoch_iter = trange(int(args.train_epochs), desc="Epoch", disable=True)
    model.zero_grad()
    for epoch in epoch_iter:
        # shuffle training data
        np.random.shuffle(data_idxs)
        for data_idx in data_idxs:
            # prepare data
            feature = features[data_idx]
            time = times[data_idx]
            label = labels[data_idx]
            # to tensor on device
            feature_tensor = torch.tensor(feature, dtype=torch.float32).to(args.device)
            time_tensor = torch.tensor(time, dtype=torch.float32).to(args.device)
            label_tensor = torch.tensor(label, dtype=torch.float32).to(args.device)

            # training
            model.train()
            loss, _, _ = model(feature_tensor, time_tensor, label_tensor)
            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
            else:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
            optimizer.step()
            model.zero_grad()
            tr_loss += loss.item()
        args.logger.info("epoch: {}; training loss: {}".format(epoch + 1, tr_loss / (epoch + 1)))
    test(args, model, features, times, labels)


def test(args, model, features, times, labels):
    y_trues, y_preds, gs_labels, pred_labels = _eval(model, features, times, labels)
    total_acc = accuracy_score(y_trues, y_preds)
    total_auc = roc_auc_score(gs_labels, pred_labels, average='micro')
    total_auc_macro = roc_auc_score(gs_labels, pred_labels, average='macro')
    args.logger.info("Train Accuracy = {:.3f}".format(total_acc))
    args.logger.info("Train AUC = {:.3f}".format(total_auc))
    args.logger.info("Train AUC Macro = {:.3f}".format(total_auc_macro))


def main(args):
    # general set up
    random.seed(13)
    np.random.seed(13)
    torch.manual_seed(13)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(13)

    model_type = args.model
    assert model_type in {'lstm', 'tlstm', 'clstm', 'ctlstm'}, \
        "we support: lstm, tlstm, clstm, and ctlstm but get {}".format(model_type)
    conf = "{}.conf".format(model_type)

    # training
    if args.do_train:
        args.logger.info("start training...")

    # prediction
    if args.do_test:
        args.logger.info("start test...")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default='lstm', type=str,
                        help="which model used for experiment. We have lstm, tlstm, clstm, and ctlstm")
    parser.add_argument("--train_data", default=None, type=str,
                        help="training data dir, should contain a feature, time, and label pickle files")
    parser.add_argument("--test_data", default=None, type=str,
                        help="test data dir, should contain a feature, time, and label pickle files")
    parser.add_argument("--model_path", default="./model", type=str, help='where to save the trained model')
    parser.add_argument("--config_path", default=None, type=str, help='where to save the config file')
    parser.add_argument("--log_file", default=None, type=str, help='log file')
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run prediction on the test set.")
    parser.add_argument("--do_test", action='store_true',
                        help="Whether to run prediction on the test set.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help='values [-mgn, mgn] to clip gradient')
    parser.add_argument("--weight_decay", default=0.0, type=float, help='weight decay used in AdamW')
    parser.add_argument("--eps", default=1e-8, type=float, help='eps for AdamW')
    parser.add_argument("--learning_rate", default=1e-3, type=float, help='learning_rate')
    parser.add_argument("--dropout_rate", default=0.1, type=float, help='drop probability')
    parser.add_argument("--train_epochs", default=50, type=int, help='number of epochs for training')
    parser.add_argument("--hidden_dim", default=100, type=int, help='TLSTM hidden layer size')
    parser.add_argument("--fc_dim", default=50, type=int, help='fully connected layer size')
    # TODO: ensemble two TLSTM for handling different data encoding format
    parser.add_argument("--do_ensemble", default=0, type=int,
                        help='wether use ensemble model to process OHE + numeric')
    # TODO: enable mix-percision training
    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument("--fp16_opt_level", type=str, default="O1",
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")

    args = parser.parse_args()
    args.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args.logger = SeqEHRLogger(logger_file=args.log_file, logger_level='i').get_logger()
    if args.config_path is None:
        args.config_path = args.model_path
    main(args)
