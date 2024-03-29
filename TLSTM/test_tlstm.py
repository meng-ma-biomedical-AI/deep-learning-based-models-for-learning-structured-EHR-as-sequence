import argparse
import random
import sys
sys.path.append("../")


from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import accuracy_score, roc_auc_score
from tqdm import trange

from tlstm import TLSTM, TLSTMConfig

from common_utils.utils import SeqEHRLogger, pkl_load, pkl_save


def _eval(model, features, times, labels, mode):
    model.eval()

    assert len(features) == len(times) == len(labels), \
        """input data and labels must have same amount of data point but 
        get num of features: {};
        get num of times: {};
        get num of labels: {}.
        """.format(len(features), len(times), len(labels))
    data_idxs = list(range(len(features)))

    y_preds, y_trues, gs_labels, pred_labels = None, None, None, None

    for data_idx in data_idxs:
        # prepare data
        feature = features[data_idx]
        time = times[data_idx]
        time = np.reshape(time, [time.shape[0], time.shape[2], time.shape[1]])
        label = labels[data_idx]
        # to tensor on device
        feature_tensor = torch.tensor(feature, dtype=torch.float32).to(args.device)
        time_tensor = torch.tensor(time, dtype=torch.float32).to(args.device)
        label_tensor = torch.tensor(label, dtype=torch.float32).to(args.device)

        with torch.no_grad():
            _, logits, y_pred = model(feature_tensor, time_tensor, label_tensor)
            logits = logits.detach().cpu().numpy()
            y_pred = y_pred.detach().cpu().numpy()
            if y_preds is None:
                pred_labels = logits
                y_preds = y_pred
                gs_labels = label
                y_trues = label[:, 1]
            else:
                pred_labels = np.concatenate([pred_labels, logits], axis=0)
                y_preds = np.concatenate([y_preds, y_pred], axis=0)
                gs_labels = np.concatenate([gs_labels, label], axis=0)
                y_trues = np.concatenate([y_trues, label[:, 1]], axis=0)

    total_acc = accuracy_score(y_trues, y_preds)
    total_auc = roc_auc_score(gs_labels, pred_labels, average='micro')
    total_auc_macro = roc_auc_score(gs_labels, pred_labels, average='macro')
    args.logger.info("{} Accuracy = {:.3f}".format(mode, total_acc))
    args.logger.info("{} AUC = {:.3f}".format(mode, total_auc))
    args.logger.info("{} AUC Macro = {:.3f}".format(mode, total_auc_macro))


def train(args, model, features, times, labels):
    assert len(features) == len(times) == len(labels), \
        """input data and labels must have same amount of data point but 
        get num of features: {};
        get num of times: {};
        get num of labels: {}.
        """.format(len(features), len(times), len(labels))
    data_idxs = list(range(len(features)))

    # optimizer set up
    # # use adam to follow the original implementation
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)

    # # using AdamW for better generalizability
    # no_decay = {'bias', 'norm'}
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
    epoch_iter = trange(int(args.train_epochs), desc="Epoch")
    model.zero_grad()
    for epoch in epoch_iter:
        # shuffle training data
        # np.random.shuffle(data_idxs)
        for data_idx in data_idxs:
            # prepare data
            feature = features[data_idx]
            time = times[data_idx]
            time = np.reshape(time, [time.shape[0], time.shape[2], time.shape[1]])
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
            else:
                loss.backward()
            optimizer.step()
            model.zero_grad()
            tr_loss += loss.item()
        args.logger.info("epoch: {}; training loss: {}".format(epoch+1, tr_loss/(epoch+1)))

    _eval(model, features, times, labels, "train")


def test(args, model, features, times, labels):
    assert len(features) == len(times) == len(labels), \
        """input data and labels must have same amount of data point but 
        get num of features: {};
        get num of times: {};
        get num of labels: {}.
        """.format(len(features), len(times), len(labels))
    data_idxs = list(range(len(features)))
    _eval(model, features, times, labels, "test")


def main(args):
    # general set up
    random.seed(13)
    np.random.seed(13)
    torch.manual_seed(13)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(13)
    conf = "tlstm.conf"

    # load training data
    if args.do_train:
        train_data = pkl_load("../data/tlstm_sync/data_train.pkl")
        train_elapsed_data = pkl_load("../data/tlstm_sync/elapsed_train.pkl")
        train_labels = pkl_load("../data/tlstm_sync/label_train.pkl")
        # init config
        input_dim = train_data[0].shape[2]
        output_dim = train_labels[0].shape[1]
        config = TLSTMConfig(input_dim, output_dim, args.hidden_dim, args.fc_dim, args.dropout_rate)
        # init TLSTM model
        model = TLSTM(config=config)
        model.to(args.device)
        # training
        train(args, model, train_data, train_elapsed_data, train_labels)
        # save model and config
        torch.save(model.state_dict(), Path(args.model_path) / "pytorch_model.bin")
        pkl_save(config, Path(args.config_path) / conf)

    # load test data
    if args.do_test:
        test_data = pkl_load("../data/tlstm_sync/data_test.pkl")
        test_elapsed_data = pkl_load("../data/tlstm_sync/elapsed_test.pkl")
        test_labels = pkl_load("../data/tlstm_sync/label_test.pkl")
        config = pkl_load(Path(args.config_path) / conf)
        model = TLSTM(config=config)
        model.load_state_dict(torch.load(Path(args.model_path) / "pytorch_model.bin"))
        model.to(args.device)
        test(args, model, test_data, test_elapsed_data, test_labels)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
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