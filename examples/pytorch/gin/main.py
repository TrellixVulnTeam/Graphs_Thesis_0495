import sys
import numpy as np
from sklearn.metrics import f1_score
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.optim as optim
import os

from pytorchtools import EarlyStopping

from dataloader import GraphDataLoader, collate
from parser import Parser
from gin import GIN
import pandas as pd
import dgl.nn.pytorch as dglnn
import torch.nn.functional as F
import dgl
from torchsummary import summary


from dgl.data.utils import save_graphs, load_graphs


config = {
    "batch_size": 32,
    "ActivityIdList":
         [{'name': 'washDishes', 'id': 0},
         {'name': 'goToBed', 'id': 1},
         {'name': 'brushTeeth', 'id': 2},
         {'name': 'prepareLunch', 'id': 3},
         {'name': 'eating', 'id': 4},
         {'name': 'takeShower', 'id': 5},
         {'name': 'leaveHouse', 'id': 6},
         {'name': 'getDrink', 'id': 7},
         {'name': 'prepareBreakfast', 'id': 8},
         {'name': 'getSnack', 'id': 9},
         {'name': 'idle', 'id': 10},
         {'name': 'grooming', 'id': 11},
         {'name': 'prepareDinner', 'id': 12},
         {'name': 'relaxing', 'id': 13},
         {'name': 'useToilet', 'id': 14}],
"merging_activties" : {
        "loadDishwasher": "washDishes",
        "unloadDishwasher": "washDishes",
        "loadWashingmachine": "washClothes",
        "unloadWashingmachine": "washClothes",
        "receiveGuest": "relaxing",
        "eatDinner": "eating",
        "eatBreakfast": "eating",
        "getDressed": "grooming",
        "shave": "grooming",
        "takeMedication": "idle",
        "leave_Home": "leaveHouse",
        "Sleeping": "goToBed",
        "Bed_to_Toilet": "useToilet",
        "Enter_Home": "idle",
        "Respirate": "relaxing",
        "Work": "idle",
        "Housekeeping": "idle",
        "Idle": "idle",
        "watchTV": "relaxing"
    },
}
def getClassnameFromID(train_label):

    ActivityIdList = config['ActivityIdList']
    train_label = [x for x in ActivityIdList if x["id"] == int(train_label)]
    return train_label[0]['name']

def train(args, net, trainloader, optimizer, criterion, epoch):
    net.train()

    running_loss = 0
    total_iters = len(trainloader)
    # setup the offset to avoid the overlap with mouse cursor
    # bar = tqdm(range(total_iters), unit='batch', position=2, file=sys.stdout)

    for (graphs, labels) in trainloader:

        # batch graphs will be shipped to device in forward part of model
        labels = labels.to(args.device)
        feat = graphs.ndata.pop('attr').to(args.device)
        graphs = graphs.to(args.device)
        outputs, _ = net(graphs, feat)



        loss = criterion(outputs, labels)
        running_loss += loss.item()
        # backprop
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    # the final batch will be aligned
    running_loss = running_loss / total_iters

    return running_loss


def eval_net(args, net, dataloader, criterion, run_config, house_name, text = 'train'):
    net.eval()

    total = 0
    total_loss = 0
    total_correct = 0
    f1 = 0
    all_labels = []
    all_predicted = []
    nb_classes = args.nb_classes
    confusion_matrix = torch.zeros(nb_classes, nb_classes)
    hiddenLayerEmbeddings = None

    for data in dataloader:
        graphs, labels = data
        feat = graphs.ndata.pop('attr').to(args.device)
        graphs = graphs.to(args.device)
        labels = labels.to(args.device)
        total += len(labels)
        outputs, hiddenLayerEmbeddings = net(graphs, feat)
        _, predicted = torch.max(outputs.data, 1)

        total_correct += (predicted == labels.data).sum().item()
        loss = criterion(outputs, labels)
        # crossentropy(reduce=True) for default
        total_loss += loss.item() * len(labels)

        all_labels.extend(labels.cpu())
        all_predicted.extend(predicted.cpu())

        for t, p in zip(labels.view(-1), predicted.view(-1)):
            confusion_matrix[t.long(), p.long()] += 1

    if args.save_embeddings:
        hiddenLayerEmbeddings = hiddenLayerEmbeddings.detach().cpu().numpy()
        hiddenLayerEmbeddings = hiddenLayerEmbeddings[1:]
        df = pd.DataFrame(hiddenLayerEmbeddings)
        df['activity'] = np.array(all_labels)
        df.to_csv("../../../data/" + house_name + "/" + run_config + "_graph_embeddings.csv", index=False)
        df.to_csv("../../../../../Research/data/" + house_name + "/" + run_config + "_graph_embeddings.csv", index=False)

    np.save('./' + text + '_confusion_matrix.npy', confusion_matrix)

    per_class_acc = confusion_matrix.diag() / confusion_matrix.sum(1)
    per_class_acc = per_class_acc.cpu().numpy()
    per_class_acc[np.isnan(per_class_acc)] = -1
    per_class_acc_dict = {}
    for i, entry in enumerate(per_class_acc):
        if entry != -1:
            per_class_acc_dict[getClassnameFromID(i)] = entry

    f1 = f1_score(all_labels, all_predicted, average='macro')


    loss, acc = 1.0*total_loss / total, 1.0*total_correct / total

    net.train()

    return loss, acc, f1, per_class_acc_dict

def getIDFromClassName(train_label, config):
    ActivityIdList = config['ActivityIdList']
    train_label = [x for x in ActivityIdList if x["name"] == train_label]
    return train_label[0]['id']


# Dataset Class
class GraphHouseDataset():
    def __init__(self, graphs, labels):
        super(GraphHouseDataset, self).__init__()
        self.graphs = graphs
        self.labels = labels

    def __getitem__(self, idx):
        """ Get graph and label by index"""
        return self.graphs[idx], self.labels[idx]

    def __len__(self):
        """Number of graphs in the dataset"""
        return len(self.graphs)


def _split_rand(labels, split_ratio=0.8, seed=0, shuffle=False):
    num_entries = len(labels)
    indices = list(range(num_entries))
    np.random.seed(seed)
    np.random.shuffle(indices)
    split = int(np.math.floor(split_ratio * num_entries))
    train_idx, valid_idx = indices[:split], indices[split:]

    print(
        "train_set : test_set = %d : %d",
        len(train_idx), len(valid_idx))

    return train_idx, valid_idx


def main(args, run_config, house_name, shuffle=False):

    # set up seeds, args.seed supported
    torch.manual_seed(seed=args.seed)
    np.random.seed(seed=args.seed)

    is_cuda = not args.disable_cuda and torch.cuda.is_available()
    is_cuda = False

    if is_cuda:
        args.device = torch.device("cuda:" + str(args.device))
        torch.cuda.manual_seed_all(seed=args.seed)
    else:
        args.device = torch.device("cpu")

    # initialize the early_stopping object
    early_stopping = EarlyStopping(patience=15, verbose=True)

    model = GIN(
        args.num_layers, args.num_mlp_layers,
        args.input_features, args.hidden_dim, args.nb_classes,
        args.final_dropout, args.learn_eps,
        args.graph_pooling_type, args.neighbor_pooling_type, args.save_embeddings).to(args.device)


    print(model)
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.5)

    file_names = ['ordonezB', 'houseB', 'houseC', 'houseA', 'ordonezA']

    if run_config is 'ob':
        graph_path = os.path.join('../../../data/all_houses/all_houses_ob.bin')
    elif run_config is 'raw':
        graph_path = os.path.join('../../../data/all_houses/all_houses_raw.bin')

    graphs = []
    labels = []

    if not os.path.exists(graph_path):
     for file_name in file_names:
        print('\n\n\n\n')
        print('*******************************************************************')
        print('\t\t\t\t\t' + file_name + '\t\t\t\t\t\t\t')
        print('*******************************************************************')
        print('\n\n\n\n')
        if run_config is 'ob':
            house = pd.read_csv('../../../data/' + file_name + '/ob_' + file_name + '.csv')
            lastChangeTimeInMinutes = pd.read_csv('../../../data/' + file_name + '/' + 'ob-house' + '-sensorChangeTime.csv')
        elif run_config is 'raw':
            house = pd.read_csv('../../../data/' + file_name + '/' + file_name + '.csv')
            lastChangeTimeInMinutes = pd.read_csv('../../../data/' + file_name + '/' + 'house' + '-sensorChangeTime.csv')

        nodes = pd.read_csv('../../../data/' + file_name + '/nodes.csv')
        edges = pd.read_csv('../../../data/' + file_name + '/bidrectional_edges.csv')


        u = edges['Src']
        v = edges['Dst']

        # Create Graph per row of the House CSV

        # Combine Feature like this: Value, Place_in_House, Type, Last_change_Time_in_Second for each node
        for i in range(len(house)):
        # for i in range(5000):
            feature = []
            flag = 0
            prev_node_value = 0
            prev_node_change_time = 0
            # Define Graph
            g = dgl.graph((u, v))
            node_num = 0
            total_nodes = len(nodes)
            # Add Features
            for j in range(total_nodes - 1):
                if nodes.loc[j, 'Type'] == 1:
                    node_value = -1
                    node_place_in_house = nodes.loc[j, 'place_in_house']
                    node_type = nodes.loc[j, 'Type']
                    feature.append([node_value, node_place_in_house, node_type, -1])
                    node_num += 1
                    continue

                if flag == 0:
                    node_value = house.iloc[i, 4 + j - node_num]
                    last_change_time_in_minutes = lastChangeTimeInMinutes.iloc[i, 4 + j - node_num]
                    node_place_in_house = nodes.loc[j, 'place_in_house']
                    node_type = nodes.loc[j, 'Type']
                    feature.append([node_value, node_place_in_house, node_type, last_change_time_in_minutes])
                    if nodes.loc[j, 'Object'] == nodes.loc[j+1, 'Object']:
                        prev_node_value = node_value
                        prev_node_change_time = last_change_time_in_minutes
                        flag = 1
                else:
                    node_num += 1
                    node_place_in_house = nodes.loc[j, 'place_in_house']
                    node_type = nodes.loc[j, 'Type']
                    feature.append([prev_node_value, node_place_in_house, node_type, prev_node_change_time])
                    if nodes.loc[j, 'Object'] != nodes.loc[j+1, 'Object']:
                        flag = 0

            feature.append([house.loc[i, 'time_of_the_day'], -1, -1, -1])
            g.ndata['attr'] = torch.tensor(feature)

        # Give Label
            try:
                mappedActivity = config['merging_activties'][house.iloc[i, 2]]
                labels.append(getIDFromClassName(mappedActivity, config))
            except:
                activity = house.iloc[i, 2]
                labels.append(getIDFromClassName(activity, config))
            graphs.append(g)

        graph_labels = {"glabel": torch.tensor(labels)}

        save_graphs(graph_path, graphs, graph_labels)

    else:
        graphs, labels = load_graphs(graph_path)
        labels = list(labels['glabel'].numpy())
    # print(np.unique(labels))
    print(len(graphs))

    total_ids = np.arange(len(labels), dtype=int)
    valid_idx = []
    for key, val in args.house_start_end_dict.items():
        if key == house_name:
            continue
        start, end = val
        valid_idx.extend(np.arange(start, end))

    if run_config is 'ob':
        config["house_start_end_dict"] = [{'ordonezB': (0, 2487)}, {'houseB': (2487, 4636)},
                                          {'houseC': (4636, 6954)}, {'houseA': (6954, 7989)},
                                          {'ordonezA': (7989, 8557)}]
    elif run_config is 'raw':
        config["house_start_end_dict"] = [{'ordonezB': (0, 30470)}, {'houseB': (30470, 51052)},
                                          {'houseC': (51052, 77539)}, {'houseA': (77539, 114626)},
                                          {'ordonezA': (114626, 134501)}]

    # ordonezB Length

    for x in range(len(config["house_start_end_dict"])):
        key,  value = list(config["house_start_end_dict"][x].items())[0]
        if key is house_name:
            start, end = config["house_start_end_dict"][x][house_name]
            break

    test_idx = np.arange(start, end)



    train_idx = list(set(total_ids) - set(valid_idx) - set(test_idx))

    train_graphs = [graphs[i] for i in train_idx]
    train_labels = [labels[i] for i in train_idx]

    val_graphs = [graphs[i] for i in valid_idx]
    val_labels = [labels[i] for i in valid_idx]

    test_graphs = [graphs[i] for i in test_idx]
    test_labels = [labels[i] for i in test_idx]


    trainDataset = GraphHouseDataset(train_graphs, train_labels)
    valDataset = GraphHouseDataset(val_graphs, val_labels)
    testDataset = GraphHouseDataset(test_graphs, test_labels)

    trainloader = GraphDataLoader(
        trainDataset, batch_size=args.batch_size, device=args.device,
        collate_fn=collate, seed=args.seed, shuffle=shuffle,
        split_name='fold10', fold_idx=args.fold_idx, save_embeddings= args.save_embeddings).train_valid_loader()

    validloader = GraphDataLoader(
        valDataset, batch_size=args.batch_size, device=args.device,
        collate_fn=collate, seed=args.seed, shuffle=shuffle,
        split_name='fold10', fold_idx=args.fold_idx, save_embeddings= args.save_embeddings).train_valid_loader()

    testloader = GraphDataLoader(
        testDataset, batch_size=args.batch_size, device=args.device,
        collate_fn=collate, seed=args.seed, shuffle=shuffle,
        split_name='fold10', fold_idx=args.fold_idx, save_embeddings=args.save_embeddings).train_valid_loader()


    # or split_name='rand', split_ratio=0.7
    criterion = nn.CrossEntropyLoss()  # default reduce is true

    for epoch in range(args.epochs):
        train(args, model, trainloader, optimizer, criterion, epoch)
        scheduler.step()

        # early_stopping needs the F1 score to check if it has increased,
        # and if it has, it will make a checkpoint of the current model

        if epoch % 10 == 9:
            print('epoch: ', epoch)
            train_loss, train_acc, train_f1_score, train_per_class_accuracy = eval_net(
                args, model, trainloader, criterion, run_config, house_name)

            print('train set - average loss: {:.4f}, accuracy: {:.0f}%  train_f1_score: {:.4f} '
                    .format(train_loss, 100. * train_acc, train_f1_score))

            # print('train per_class accuracy', test_per_class_accuracy)

            valid_loss, valid_acc, val_f1_score, val_per_class_accuracy = eval_net(
                args, model, validloader, criterion, run_config, house_name, text='val')

            print('valid set - average loss: {:.4f}, accuracy: {:.0f}% val_f1_score {:.4f}:  '
                    .format(valid_loss, 100. * valid_acc, val_f1_score))

            test_loss, test_acc, test_f1_score, test_per_class_accuracy = eval_net(
                args, model, testloader, criterion, run_config, house_name)

            print('test set - average loss: {:.4f}, accuracy: {:.0f}%  test_f1_score: {:.4f} '
                  .format(test_loss, 100. * test_acc, test_f1_score))

            # print('val per_class accuracy', val_per_class_accuracy)

            # early_stopping needs the validation loss to check if it has decresed,
            # and if it has, it will make a checkpoint of the current model
            early_stopping(val_f1_score, model)

            if early_stopping.early_stop:
                print("Early stopping")
                break

    args.save_embeddings = True
    model = GIN(
        args.num_layers, args.num_mlp_layers,
        args.input_features, args.hidden_dim, args.nb_classes,
        args.final_dropout, args.learn_eps,
        args.graph_pooling_type, args.neighbor_pooling_type, args.save_embeddings).to(args.device)
    model.eval()

    # making loader here because weighted sampler is off for testing and it is on for other parts.
    # Since we want embeddings in order so sampler is off for testing.
    testDataset = GraphHouseDataset(test_graphs, test_labels)
    testloader = GraphDataLoader(
        testDataset, batch_size=args.batch_size, device=args.device,
        collate_fn=collate, seed=args.seed, shuffle=shuffle,
        split_name='fold10', fold_idx=args.fold_idx, save_embeddings=args.save_embeddings).train_valid_loader()

    if args.save_embeddings:
        if os.path.exists('./checkpoint.pth'):
            print('loading saved checkpoint')
            state = torch.load('./checkpoint.pth')
            model.load_state_dict(state)
            # model.load_state_dict(state['state_dict'])
            # optimizer.load_state_dict(state['optimizer'])
    test_loss, test_acc, test_f1_score, test_per_class_accuracy = eval_net(
        args, model, testloader, criterion, run_config, house_name)

    house_results_dictionary = {}
    house_results_dictionary['accuracy'] = test_acc

    house_results_dictionary['f1_score'] = test_f1_score

    house_results_dictionary['test_per_class_accuracy'] = test_per_class_accuracy

    print('test set - average loss: {:.4f}, accuracy: {:.0f}%  test_f1_score: {:.4f} '
          .format(test_loss, 100. * test_acc, test_f1_score))

    return house_results_dictionary


if __name__ == '__main__':
    args = Parser(description='GIN').args
    print('show all arguments configuration...')
    print(args)
    if not os.path.exists(os.path.join('../../../logs', 'graph_classification')):
        os.mkdir(os.path.join('../../../logs', 'graph_classification'))

    # for run_config in ['ob', 'raw']:
    for run_config in ['raw']:
        results_list = []
        for house_name in ['ordonezB', 'houseB', 'houseC', 'houseA', 'ordonezA']:
            print(house_name, '\n\n')
            # Train and then Save embeddings
            args.save_embeddings = False
            house_results_dictionary = main(args, run_config, house_name,  shuffle=False)
            results_list.append(house_results_dictionary)
            print('saving..... ', house_name)
            np.save(os.path.join('../../../logs/graph_classification', run_config + '.npy'), results_list)



