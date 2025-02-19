import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import numpy as np

device = torch.device("cuda:{}".format(2) if torch.cuda.is_available() else "cpu")


class conv2d_(nn.Module):
    def __init__(self, input_dims, output_dims, kernel_size, stride=(1, 1),
                 padding='SAME', use_bias=True, activation=F.relu,
                 bn_decay=None):
        super(conv2d_, self).__init__()
        self.activation = activation
        if padding == 'SAME':
            self.padding_size = math.ceil(kernel_size)
        else:
            self.padding_size = [0, 0]
        # print("input channel: ", input_dims)
        self.conv = nn.Conv2d(input_dims, output_dims, kernel_size, stride=stride,
                              padding=0, bias=use_bias)
        self.batch_norm = nn.BatchNorm2d(output_dims, momentum=bn_decay)
        torch.nn.init.xavier_uniform_(self.conv.weight)

        if use_bias:
            torch.nn.init.zeros_(self.conv.bias)

    def forward(self, x):
        x = x.permute(0, 3, 2, 1)
        x = F.pad(x, ([self.padding_size[1], self.padding_size[1], self.padding_size[0], self.padding_size[0]]))
        x = self.conv(x)
        x = self.batch_norm(x)
        if self.activation is not None:
            x = F.relu_(x)
        return x.permute(0, 3, 2, 1)


class FC(nn.Module):
    def __init__(self, input_dims, units, activations, bn_decay, use_bias=True):
        super(FC, self).__init__()
        if isinstance(units, int):
            units = [units]
            input_dims = [input_dims]
            activations = [activations]
        elif isinstance(units, tuple):
            units = list(units)
            input_dims = list(input_dims)
            activations = list(activations)
        assert type(units) == list
        self.convs = nn.ModuleList([conv2d_(
            input_dims=input_dim, output_dims=num_unit, kernel_size=[1, 1], stride=[1, 1],
            padding='VALID', use_bias=use_bias, activation=activation,
            bn_decay=bn_decay) for input_dim, num_unit, activation in
            zip(input_dims, units, activations)])

    def forward(self, x):
        # print("before x:", x.shape)
        for conv in self.convs:
            x = conv(x)
        # print("after x: ", x.shape)
        return x


class STEmbedding(nn.Module):
    '''
    spatio-temporal embedding
    SE:     [num_vertex, D]
    TE:     [batch_size, num_his + num_pred, 2] (dayofweek, timeofday)
    T:      num of time steps in one day
    D:      output dims
    retrun: [batch_size, num_his + num_pred, num_vertex, D]
    '''

    def __init__(self, input_dim, D, bn_decay, gpu=1):
        self.gpu = gpu
        super(STEmbedding, self).__init__()

        self.FC_te = FC(
            input_dims=[input_dim, D], units=[D, D], activations=[F.relu, None],
            bn_decay=bn_decay)  # input_dims = time step per day + days per week=288/1440+7=295/1447

    def forward(self, SE, TE, T):
        # spatial embedding
        SE = SE.unsqueeze(0).unsqueeze(0)
        # temporal embedding
        dayofweek = torch.empty(TE.shape[0], TE.shape[1], 7)
        timeofday = torch.empty(TE.shape[0], TE.shape[1], T)
        for i in range(TE.shape[0]):
            dayofweek[i] = F.one_hot(TE[..., 0][i].to(torch.int64) % 7, 7)
        for j in range(TE.shape[0]):
            timeofday[j] = F.one_hot(TE[..., 1][j].to(torch.int64) % T, T)
        TE = torch.cat((dayofweek, timeofday), dim=-1)
        TE = TE.unsqueeze(dim=2)
        # print("TE: ", TE)
        if self.gpu:
            if self.gpu == 1:
                TE = TE.cuda()
            else:
                TE = TE.to(device)
        TE = self.FC_te(TE)
        del dayofweek, timeofday
        return SE + TE


class MAB(nn.Module):
    def __init__(self, K, d, input_dim, output_dim, bn_decay):
        super(MAB, self).__init__()
        D = K * d
        self.K = K
        self.d = d
        self.FC_q = FC(input_dims=input_dim, units=D, activations=F.relu,
                       bn_decay=bn_decay)
        self.FC_k = FC(input_dims=input_dim, units=D, activations=F.relu,
                       bn_decay=bn_decay)
        self.FC_v = FC(input_dims=input_dim, units=D, activations=F.relu,
                       bn_decay=bn_decay)
        self.FC = FC(input_dims=D, units=output_dim, activations=F.relu,
                     bn_decay=bn_decay)

    def forward(self, Q, K, batch_size, type="spatial", mask=None):
        query = self.FC_q(Q)
        key = self.FC_k(K)
        value = self.FC_v(K)
        query = torch.cat(torch.split(query, self.K, dim=-1), dim=0)
        key = torch.cat(torch.split(key, self.K, dim=-1), dim=0)
        value = torch.cat(torch.split(value, self.K, dim=-1), dim=0)
        if mask == None:
            if type == "temporal":
                query = query.permute(0, 2, 1, 3)
                key = key.permute(0, 2, 1, 3)
                value = value.permute(0, 2, 1, 3)
            attention = torch.matmul(query, key.transpose(2, 3))
            attention /= (self.d ** 0.5)
            attention = F.softmax(attention, dim=-1)
            result = torch.matmul(attention, value)
            if type == "temporal":
                result = result.permute(0, 2, 1, 3)
            result = torch.cat(torch.split(result, batch_size, dim=0), dim=-1)  # orginal K, change to batch_size
            result = self.FC(result)
        else:
            mask = torch.cat(torch.split(mask, self.K, dim=-1), dim=0)
            if type == "temporal":
                query = query.permute(0, 2, 1, 3)
                key = key.permute(0, 2, 1, 3)
                value = value.permute(0, 2, 1, 3)
                mask = mask.permute(0, 2, 1, 3)
            if mask.shape == query.shape:
                set_mask = torch.ones_like(key).cuda()
                mask = torch.matmul(mask, set_mask.transpose(2, 3))
            elif mask.shape == key.shape:
                set_mask = torch.ones_like(query).cuda()
                mask = torch.matmul(set_mask, mask.transpose(2, 3))
            attention = torch.matmul(query, key.transpose(2, 3))
            attention /= (self.d ** 0.5)
            attention = attention.masked_fill(mask == 0, -1e9)
            attention = F.softmax(attention, dim=-1)
            result = torch.matmul(attention, value)
            if type == "temporal":
                result = result.permute(0, 2, 1, 3)
            result = torch.cat(torch.split(result, batch_size, dim=0), dim=-1)  # orginal K, change to batch_size
            result = self.FC(result)
        return result


class spatialAttention(nn.Module):
    '''
    spatial attention mechanism
    X:      [batch_size, num_step, num_vertex, D]
    STE:    [batch_size, num_step, num_vertex, D]
    K:      number of attention heads
    d:      dimension of each attention outputs
    return: [batch_size, num_step, num_vertex, D]
    '''

    def __init__(self, K, d, num_his, set_dim, bn_decay):
        super(spatialAttention, self).__init__()
        D = K * d
        self.d = d
        self.K = K
        self.num_his = num_his
        self.set_dim = set_dim
        self.I = nn.Parameter(torch.Tensor(1, num_his, set_dim, 2 * D))
        nn.init.xavier_uniform_(self.I)
        self.mab0 = MAB(K, d, 2 * D, 2 * D, bn_decay)
        self.mab1 = MAB(K, d, 2 * D, D, bn_decay)

    def forward(self, X, STE, mask):
        batch_size = X.shape[0]
        # print(X.shape, STE.shape)
        X = torch.cat((X, STE), dim=-1)
        # [batch_size, num_step, num_vertex, K * d]
        I = self.I.repeat(X.size(0), 1, 1, 1)
        H = self.mab0(I, X, batch_size, "spatial", mask)
        result = self.mab1(X, H, batch_size, "spatial", mask)
        return result


class temporalAttention(nn.Module):
    '''
    temporal attention mechanism
    X:      [batch_size, num_step, num_vertex, D]
    STE:    [batch_size, num_step, num_vertex, D]
    K:      number of attention heads
    d:      dimension of each attention outputs
    return: [batch_size, num_step, num_vertex, D]
    '''

    def __init__(self, K, d, num_of_vertices, set_dim, bn_decay):
        super(temporalAttention, self).__init__()
        D = K * d
        self.d = d
        self.K = K
        self.num_of_vertices = num_of_vertices
        self.set_dim = set_dim
        self.I = nn.Parameter(torch.Tensor(1, set_dim, self.num_of_vertices, 2 * D))
        nn.init.xavier_uniform_(self.I)
        self.mab0 = MAB(K, d, 2 * D, 2 * D, bn_decay)
        self.mab1 = MAB(K, d, 2 * D, D, bn_decay)

    def forward(self, X, STE, mask):
        batch_size = X.shape[0]
        X = torch.cat((X, STE), dim=-1)
        # [batch_size, num_step, num_vertex, K * d]
        I = self.I.repeat(X.size(0), 1, 1, 1)
        H = self.mab0(I, X, batch_size, "temporal", mask)
        result = self.mab1(X, H, batch_size, "temporal", mask)
        return result


class STAttBlock(nn.Module):
    def __init__(self, K, d, num_his, num_of_vertices, set_dim, bn_decay):
        super(STAttBlock, self).__init__()
        self.spatialAttention = spatialAttention(K, d, num_his, set_dim, bn_decay)
        self.temporalAttention = temporalAttention(K, d, num_of_vertices, set_dim, bn_decay)

    def forward(self, X, STE, mask):
        HS = self.spatialAttention(X, STE, mask)
        HT = self.temporalAttention(X, STE, mask)
        H = HS + HT
        del HS, HT
        return torch.add(X, H)


class STAttBlock_self(nn.Module):
    def __init__(self, K, d, num_his, num_of_vertices, set_dim, bn_decay):
        super(STAttBlock_self, self).__init__()
        self.spatialAttention = spatialAttention(K, d, num_his, set_dim, bn_decay)
        self.temporalAttention = temporalAttention(K, d, num_of_vertices, set_dim, bn_decay)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, 1, K * d))
        nn.init.xavier_uniform_(self.mask_token)

    def forward(self, X, STE, mask):
        if mask != None:
            zero_shape = torch.zeros_like(X).cuda()
            mask_value = zero_shape + self.mask_token
            X = mask * X + (1 - mask) * mask_value
        HS = self.spatialAttention(X, STE, mask)
        HT = self.temporalAttention(X, STE, mask)
        H = HS + HT
        del HS, HT
        return torch.add(X, H)


class transformAttention(nn.Module):
    '''
    transform attention mechanism
    X:        [batch_size, num_his, num_vertex, D]
    STE_his:  [batch_size, num_his, num_vertex, D]
    STE_pred: [batch_size, num_pred, num_vertex, D]
    K:        number of attention heads
    d:        dimension of each attention outputs
    return:   [batch_size, num_pred, num_vertex, D]
    '''

    def __init__(self, K, d, bn_decay):
        super(transformAttention, self).__init__()
        D = K * d
        self.K = K
        self.d = d
        self.FC_q = FC(input_dims=D, units=D, activations=F.relu,
                       bn_decay=bn_decay)
        self.FC_k = FC(input_dims=D, units=D, activations=F.relu,
                       bn_decay=bn_decay)
        self.FC_v = FC(input_dims=D, units=D, activations=F.relu,
                       bn_decay=bn_decay)
        self.FC = FC(input_dims=D, units=D, activations=F.relu,
                     bn_decay=bn_decay)

    def forward(self, X, STE_his, STE_pred):
        batch_size = X.shape[0]
        # [batch_size, num_step, num_vertex, K * d]
        query = self.FC_q(STE_pred)
        key = self.FC_k(STE_his)
        value = self.FC_v(X)
        # [K * batch_size, num_step, num_vertex, d]
        query = torch.cat(torch.split(query, self.K, dim=-1), dim=0)
        key = torch.cat(torch.split(key, self.K, dim=-1), dim=0)
        value = torch.cat(torch.split(value, self.K, dim=-1), dim=0)
        # query: [K * batch_size, num_vertex, num_pred, d]
        # key:   [K * batch_size, num_vertex, d, num_his]
        # value: [K * batch_size, num_vertex, num_his, d]
        query = query.permute(0, 2, 1, 3)
        key = key.permute(0, 2, 3, 1)
        value = value.permute(0, 2, 1, 3)
        # [K * batch_size, num_vertex, num_pred, num_his]
        attention = torch.matmul(query, key)
        attention /= (self.d ** 0.5)
        attention = F.softmax(attention, dim=-1)
        # [batch_size, num_pred, num_vertex, D]
        X = torch.matmul(attention, value)
        X = X.permute(0, 2, 1, 3)
        X = torch.cat(torch.split(X, batch_size, dim=0), dim=-1)
        X = self.FC(X)
        del query, key, value, attention
        return X


class SSTBAN(nn.Module):
    '''
    GMAN
        X：       [batch_size, num_his, num_vertx, feature]
        TE：      [batch_size, num_his + num_pred, 2] (time-of-day, day-of-week)
        SE：      [num_vertex, K * d]
        num_his： number of history steps
        num_pred：number of prediction steps
        T：       one day is divided into T steps
        L：       number of STAtt blocks in the encoder/decoder
        K：       number of attention heads
        d：       dimension of each attention head outputs
        return：  [batch_size, num_pred, num_vertex]
    '''

    def __init__(self, args, bn_decay):
        super(SSTBAN, self).__init__()
        data_config = args['Data']
        training_config = args['Training']
        gpu = int(training_config['gpu'])
        L = int(training_config['L'])
        K = int(training_config['K'])
        d = int(training_config['d'])
        self.L = L
        self.K = K
        self.d = d
        self.node_miss_rate = float(training_config['node_miss_rate'])
        self.T_miss_len = int(training_config['T_miss_len'])
        print('L', self.L)
        print('K', self.K)
        print('d', self.d)
        print('node_miss_rate', self.node_miss_rate)
        print('T_miss_len', self.T_miss_len)
        D = K * d
        set_dim = int(training_config['reference'])
        self.num_his = int(training_config['num_his'])
        time_slice_size = int(data_config['time_slice_size'])
        self.input_dim = int(1440 / time_slice_size) + 7
        self.num_pred = int(training_config['num_pred'])
        self.num_of_vertices = int(data_config['num_of_vertices'])
        self.SE = nn.Parameter(torch.FloatTensor(self.num_of_vertices, D))
        self.STEmbedding = STEmbedding(self.input_dim, D, bn_decay, gpu)
        self.STAttBlock_1 = nn.ModuleList(
            [STAttBlock(K, d, self.num_his, self.num_of_vertices, set_dim, bn_decay) for _ in range(L)])
        self.STAttBlock_self = nn.ModuleList(
            [STAttBlock_self(K, d, self.num_his, self.num_of_vertices, set_dim, bn_decay) for _ in range(1)])
        self.STAttBlock_2 = nn.ModuleList(
            [STAttBlock(K, d, self.num_pred, self.num_of_vertices, set_dim, bn_decay) for _ in range(L)])
        self.transformAttention = transformAttention(K, d, bn_decay)
        self.dataset = data_config['dataset_name']
        in_channels = int(training_config['in_channels'])
        out_channels = int(training_config['out_channels'])


        #add
        # self.FC_1 = FC(input_dims=[in_channels, D], units=[D, D], activations=[F.relu, None],
        #                bn_decay=bn_decay)  # in_channels=3
        # self.FC_2 = FC(input_dims=[D, D], units=[D, out_channels], activations=[F.relu, None],
        #                bn_decay=bn_decay)
        self.FC_1 = FC(input_dims=[in_channels, D], units=[D, 64], activations=[F.relu, None],
                       bn_decay=bn_decay)  # in_channels=3
        self.FC_2 = FC(input_dims=[D, D], units=[D, out_channels], activations=[F.relu, None],
                       bn_decay=bn_decay)
        self.adaptive_embedding = nn.init.xavier_uniform_(
            nn.Parameter(torch.empty(24, 307, 64))
            #[in_steps, num_nodes, adaptive_embedding_dim]
        )
        #add

    def forward(self, X, TE, mode, mask=None):

        # input
        X = self.FC_1(X)

        #add
        adp_emb = self.adaptive_embedding.expand(
            size=(X.shape[0], *self.adaptive_embedding.shape)
        )
        X = torch.cat((X, adp_emb), dim=-1)
        #add

        # STE
        STE = self.STEmbedding(self.SE, TE, self.input_dim - 7)
        STE_his = STE[:, :self.num_his]
        STE_pred = STE[:, self.num_his:]
        T_miss_len = self.T_miss_len

        if mode == 'train':

            batch_mask_matrix = np.random.rand(X.shape[0], int(X.shape[1] / T_miss_len), X.shape[2],
                                               X.shape[3]) < self.node_miss_rate
            batch_mask_matrix = torch.from_numpy(np.repeat(batch_mask_matrix, T_miss_len, axis=1)).to(
                torch.float32).cuda()
            X_miss = X
            # encoder
            for net in self.STAttBlock_1:
                X = net(X, STE_his, None)
            complete_X_enc = X
            # transAtt
            X = self.transformAttention(X, STE_his, STE_pred)
            # decoder
            for net in self.STAttBlock_2:
                X = net(X, STE_pred, None)
            # output
            Pred = self.FC_2(X)
            # encoder
            for net in self.STAttBlock_1:
                X_miss = net(X_miss, STE_his, batch_mask_matrix)
            #self decoder
            for net in self.STAttBlock_self:
                X_miss = net(X_miss, STE_his, batch_mask_matrix)
            del STE, STE_his, STE_pred
            return Pred, complete_X_enc, X_miss
        else:
            if mask == None:
                # encoder
                for net in self.STAttBlock_1:
                    X = net(X, STE_his, None)
                # transAtt
                X = self.transformAttention(X, STE_his, STE_pred)
                # decoder
                for net in self.STAttBlock_2:
                    X = net(X, STE_pred, None)
                # output
                Pred = self.FC_2(X)
                return Pred, None

            else:
                for net in self.STAttBlock_1:
                    X = net(X, STE_his, mask)
                    # self decoder
                for net in self.STAttBlock_self:
                    X = net(X, STE_his, mask)
                # transAtt
                X = self.transformAttention(X, STE_his, STE_pred)
                # decoder
                for net in self.STAttBlock_2:
                    X = net(X, STE_pred, None)
                # output
                Pred = self.FC_2(X)
                return Pred, None


def make_model(config, bn_decay=0.1):
    model = SSTBAN(config, bn_decay=0.1)
    # init
    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    return model
