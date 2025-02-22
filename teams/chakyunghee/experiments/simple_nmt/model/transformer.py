import torch
import torch.nn as nn

import simple_nmt.data_loader as data_loader
from simple_nmt.search import SingleBeamSearchBoard



class Attention(nn.Module):

    def __init__(self):
        super().__init__()
        self.softmax = nn.Softmax(dim=-1)
    
    def forward(self, Q, K, V, mask=None, dk=64):
        w = torch.bmm(Q, K.transpose(1, 2))
        if mask is not None:
            assert w.size() == mask.size()
            w.masked_fill_(mask, -float('inf'))

        w = self.softmax(w/(dk**.5))
        c = torch.bmm(w, V)

        return c


class MultiHead(nn.Module):

    def __init__(self, hidden_size, n_splits):
        super().__init__()
        self.hidden_size = hidden_size
        self.n_splits = n_splits

        self.Q_linear = nn.Linear(hidden_size, hidden_size, bias=False)
        self.K_linear = nn.Linear(hidden_size, hidden_size, bias=False)
        self.V_linear = nn.Linear(hidden_size, hidden_size, bias=False)
        self.linear = nn.Linear(hidden_size, hidden_size, bias=False)

        self.attn = Attention()
    
    def forward(self, Q, K, V, mask=None):
        QWs = self.Q_linear(Q).split(self.hidden_size // self.n_splits, dim=-1)
        KWs = self.K_linear(K).split(self.hidden_size // self.n_splits, dim=-1)
        VWs = self.V_linear(V).split(self.hidden_size // self.n_splits, dim=-1)

        QWs = torch.cat(QWs, dim=0)
        KWs = torch.cat(KWs, dim=0)
        VWs = torch.cat(VWs, dim=0)

        if mask is not None:
            mask = torch.cat([mask for _ in range(self.n_splits)], dim=0)

        c = self.attn(
            QWs, KWs, VWs,
            mask = mask,
            dk = self.hidden_size // self.n_splits
        )
        c = c.split(Q.size(0), dim=0)
        c = self.linear(torch.cat(c, dim=-1))
        
        return c


class EncoderBlock(nn.Module):

    def __init__(
        self,
        hidden_size,
        n_splits,
        dropout_p=.1,
    ):
        super().__init__()
        self.attn = MultiHead(hidden_size, n_splits)    
        self.attn_norm = nn.LayerNorm(hidden_size)
        self.attn_dropout = nn.Dropout(dropout_p)

        self.fc = nn.Sequential(
            nn.Linear(hidden_size, hidden_size*4),
            nn.ReLU(),
            nn.Linear(hidden_size*4, hidden_size)
        )
        
        self.fc_norm = nn.LayerNorm(hidden_size)
        self.fc_dropout = nn.Dropout(dropout_p)

    def forward(self, x, mask):
        z = self.attn_norm(x)                       # pre_LayerNorm (before MultiheadAttention)
        z = x + self.attn_dropout(self.attn(Q=z,    # dropout before residual connection
                                            K=z,
                                            V=z,
                                            mask=mask))
        z = z + self.fc_dropout(self.fc(self.fc_norm(z)))

        return z, mask


class DecoderBlock(nn.Module):

    def __init__(
        self,
        hidden_size,
        n_splits,
        dropout_p=.1,
    ):
        super().__init__()
        self.masked_attn = MultiHead(hidden_size, n_splits)     # self attention in decoder
        self.masked_attn_norm = nn.LayerNorm(hidden_size)
        self.masked_attn_dropout = nn.Dropout(dropout_p)

        self.attn = MultiHead(hidden_size, n_splits)            # attention to encoder in decoder
        self.attn_norm = nn.LayerNorm(hidden_size)
        self.attn_dropout = nn.Dropout(dropout_p)

        self.fc = nn.Sequential(
            nn.Linear(hidden_size, hidden_size*4),
            nn.ReLU(),
            nn.Linear(hidden_size*4, hidden_size)
        )
        self.fc_norm = nn.LayerNorm(hidden_size)
        self.fc_dropout = nn.Dropout(dropout_p)


    def forward(self, x, key_and_value, mask, prev, future_mask):   # prev: 

        if prev is None:    # training mode
            z = self.masked_attn_norm(x)
            z = x + self.masked_attn_dropout(
                self.masked_attn(z,z,z, mask=future_mask)
            )
        else:               # inference mode
            normed_prev = self.masked_attn_norm(prev)
            z = self.masked_attn_norm(x)
            z = x + self.masked_attn_dropout(
                self.masked_attn(z, normed_prev, normed_prev, mask=None)
            )
        normed_key_and_value = self.attn_norm(key_and_value)
        z = z + self.attn_dropout(self.attn(Q=self.attn_norm(z),
                                            K=normed_key_and_value,
                                            V=normed_key_and_value,
                                            mask=mask))

        z = z + self.fc_dropout(self.fc(self.fc_norm(z)))       # LayerNorm before FeedFoward
                                                                # dropout before residual
        return z, key_and_value, mask, prev, future_mask

class MySequential(nn.Sequential):

    def forward(self, *x):      # 입력, 출력 튜플. 여러개 받도록, nn.Sequential 상속 + forward함수
        for module in self._modules.values():
            x = module(*x)
        
        return x


class Transformer(nn.Module):

    def __init__(
        self,
        input_size,
        hidden_size,
        output_size,
        n_splits,
        n_enc_blocks=6,
        n_dec_blocks=6,
        dropout_p=.1,
        max_length=512
    ):
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_splits = n_splits
        self.n_enc_blocks = n_enc_blocks
        self.n_dec_blocks = n_dec_blocks
        self.dropout_p = dropout_p
        self.max_length = max_length

        super().__init__()
        self.emb_enc = nn.Embedding(input_size, hidden_size)
        self.emb_dec = nn.Embedding(output_size, hidden_size)
        self.emb_dropout = nn.Dropout(dropout_p)

        self.pos_enc = self._generate_pos_enc(hidden_size, max_length)  # 엄청 큰거 만들어놓고 필요한 부분만큼 잘라서 쓸.

        self.encoder = MySequential(
            *[EncoderBlock(
                hidden_size,
                n_splits,
                dropout_p
            ) for _ in range(n_enc_blocks)]
        )
        self.decoder = MySequential(
            *[DecoderBlock(
                hidden_size,
                n_splits,
                dropout_p
                ) for _ in range(n_dec_blocks)]
        )
        self.generator = nn.Sequential(
            nn.LayerNorm(hidden_size),          # only for pre-LN transformer
            nn.Linear(hidden_size, output_size),
            nn.LogSoftmax(dim=-1)
        )

    @torch.no_grad()
    def _generate_pos_enc(self, hidden_size, max_length):
        enc = torch.FloatTensor(max_length, hidden_size).zero_()
        pos = torch.arange(0, max_length).unsqueeze(-1).float()
        dim = torch.arange(0, hidden_size //2).unsqueeze(0).float()

        enc[:, 0::2] = torch.sin(pos/1e+4**dim.div(float(hidden_size)))
        enc[:, 1::2] = torch.cos(pos/1e+4**dim.div(float(hidden_size)))

        return enc
    
    def _position_encoding(self, x, init_pos=0):
        assert x.size(-1) == self.pos_enc.size(-1)
        assert x.size(1) + init_pos <= self.max_length

        pos_enc = self.pos_enc[init_pos:init_pos + x.size(1)].unsqueeze(0)  # 추론시 decoder에 하나씩 들어가므로 position 잡아주기
        x = x + pos_enc.to(x.device)

        return x

    @torch.no_grad()
    def _generate_mask(self, x, length):
        mask = []
        
        max_length = max(length)
        for l in length:
            if max_length - l > 0:
                mask += [torch.cat([x.new_ones(1, l).zero_(),
                                    x.new_ones(1, (max_length - l))
                                    ], dim=-1)]
            else:
                mask += [x.new_ones(1, l).zero_()]
        
        mask = torch.cat(mask, dim=0).bool()

        return mask

    
    def forward(self, x, y):
        with torch.no_grad():
            mask = self._generate_mask(x[0], x[1])  # x[0]: encoding onehot tensor, x[1]: minibatch sample별 time-step
            x = x[0]

            mask_enc = mask.unsqueeze(1).expand(*x.size(), mask.size(-1))   # encoder에서 self-attention
            mask_dec = mask.unsqueeze(1).expand(*y.size(), mask.size(-1))   # decoder에서 encoder로 attention 

        z = self.emb_dropout(self._position_encoding(self.emb_enc(x)))
        z, _ = self.encoder(z, mask_enc)


        with torch.no_grad():
            future_mask = torch.triu(x.new_ones((y.size(1), y.size(1))), diagonal=1).bool()
            future_mask = future_mask.unsqueeze(0).expand(y.size(0), *future_mask.size())

        h = self.emb_dropout(self._position_encoding(self.emb_dec(y)))
        h, _, _, _, _ = self.decoder(h, z, mask_dec, None, future_mask)

        y_hat = self.generator(h)

        return y_hat

    # inference
    def search(self, x, is_greedy=True, max_length=255):    # search하는 방법: greedy decoding or random sampling
        batch_size = x[0].size(0)
        mask = self._generate_mask(x[0], x[1])  # encoder의 빈 time-step에 pad가 있는 곳에 mask
        x = x[0]

        mask_enc = mask.unsqueeze(1).expand(mask.size(0), x.size(1), mask.size(-1))      # (bs, n, n)
        mask_dec = mask.unsqueeze(1)            # (bs, 1, n), 왜냐면 추론은 한 time-step씩 이루어지므로                            

        z = self.emb_dropout(self._position_encoding(self.emb_enc(x)))
        z, _ = self.encoder(z, mask_enc)        # (bs, n, hs), encoder 결과값 z에 attention 적용

        y_t_1 = x.new(batch_size, 1).zero_() + data_loader.BOS  # decoder embedding y의 첫 timestep엔 BOS 넣어야함
        is_decoding = x.new_ones(batch_size, 1).bool()  # EOS가 안나온 경우: is_decoding True (decoding중)
                                                        # EOS가 나온 경우: is_decoding False  (decoding끝)      
        prevs = [None for _ in range(len(self.decoder._modules) + 1)]   # 이전 time-step의 결과물들 넣는 list
        y_hats, indice = [], []     # 앞으로 생성할 것들 저장할 [] initialize

        while is_decoding.sum() > 0 and len(indice) < max_length:
            h_t = self.emb_dropout(
                self._position_encoding(self.emb_dec(y_t_1), init_pos=len(indice))
            )
            if prevs[0] is None:
                prevs[0] = h_t      # (bs, 1, hs)
            else:
                prevs[0] = torch.cat([prevs[0], h_t], dim=1)

            for layer_index, block in enumerate(self.decoder._modules.values()):
                prev = prevs[layer_index]                               # h_t: 이전 layer로부터 온 결과값
                                                                        # mask_dec: encoder source sentence의 빈 timestep의 mask
                h_t, _, _, _, _ = block(h_t, z, mask_dec, prev, None)   # future mask는 none
                                                                        # z: encoder output, key and value
                if prevs[layer_index + 1] is None:
                    prevs[layer_index + 1] = h_t
                else:                                # 이번 lyaer에 나온 결과값 h_t를 concat해서 붙인 후 다음 attention에 씀
                    prevs[layer_index + 1] = torch.cat([prevs[layer_index + 1], h_t], dim=1)

            y_hat_t = self.generator(h_t)   # 단어별 로그 확률값

            y_hats += [y_hat_t]     # 모으기
            if is_greedy:
                y_t_1 = torch.topk(y_hat_t, 1, dim=-1)[1].squeeze(-1)
            else:       # random sampling
                y_t_1 = torch.multinomial(y_hat_t.exp().view(x.size(0), -1), 1)
            # y_t_1이 다음 time-step에 또 쓰이기위해 위로 올라감
            y_t_1 = y_t_1.masked_fill_( # 이전 time-step에 EOS가 나온(false) 경우, decoding 끝난 애들은 pad
                ~is_decoding,   
                data_loader.PAD
            )
            # 이번 time-step에 EOS인 경우, 해당 sample에 대해 false로 바꾸어서 다음 코드 돌 때 이 sample에 pad 걸리게 됨                                                              
            is_decoding = is_decoding * torch.ne(y_t_1, data_loader.EOS)    
            indice += [y_t_1]

        y_hats = torch.cat(y_hats, dim=1)
        indice = torch.cat(indice, dim=-1)

        return y_hats, indice

    def batch_beam_search(
        self,
        x,
        beam_size=5,
        max_length=255,
        n_best=1,       # 기존 search는 1 batch_beam_search면 적어도 beam_size만큼
        length_penalty=.2
    ):
        batch_size = x[0].size(0)
        n_dec_layers = len(self.decoder._modules)

        mask = self._generate_mask(x[0], x[1])
        x = x[0]

        mask_enc = mask.unsqueeze(1).expand(mask.size(0), x.size(1), mask.size(-1))
        mask_dec = mask.unsqueeze(1)

        z = self.emb_dropout(self._position_encoding(self.emb_enc(x)))
        z, _ = self.encoder(z, mask_enc)

        prev_status_config = {}
        for layer_index in range(n_dec_layers + 1):
            prev_status_config['[rev_state_%d' % layer_index] = {
                'init_status': None,
                'batch_dim_index': 0
            }
        
        boards = [
            SingleBeamSearchBoard(
                z.device,
                prev_status_config,
                beam_size=beam_size,
                max_length=max_length
            ) for _ in range(batch_size)
        ]
        done_cnt = [board.is_done() for board in boards]

        length = 0
        while sum(done_cnt) < batch_size and length <= max_length:
            fab_input, fab_z, fab_mask = [], [], []
            fab_prevs = [[] for _ in range(n_dec_layers + 1)]

            for i, board in enumerate(boards):
                if board.is_done() == 0:
                    y_hat_i, prev_status = board.get_batch()

                    fab_input += [y_hat_i                 ]
                    fab_z     += [z[i].unsqueeze(0)       ] * beam_size
                    fab_mask  += [mask_dec[i].unsqueeze(0)] * beam_size

                    for layer_index in range(n_dec_layers + 1):
                        prev_i = prev_status['prev_state_%d' % layer_index]
                        if prev_i is not None:
                            fab_prevs[layer_index] += [prev_i]
                        else:
                            fab_prevs[layer_index] = None

            fab_input = torch.cat(fab_input, dim=0)
            fab_z     = torch.cat(fab_z,     dim=0)
            fab_mask  = torch.cat(fab_mask,  dim=0)
            for i, fab_prev in enumerate(fab_prevs):
                if fab_prev is not None:
                    fab_prevs[i] = torch.cat(fab_prev, dim=0)

            h_t = self.emb_dropout(
                self._position_encoding(self.emb_dec(fab_input), init_pos=length)
            )
            if fab_prevs[0] is None:
                fab_prevs[0] = h_t
            else:
                fab_prevs[0] = torch.cat([fab_prevs[0], h_t], dim=1)

            for layer_index, block in enumerate(self.decoder._modules.values()):
                prev = fab_prevs[layer_index]

                h_t, _, _, _, _ = block(h_t, fab_z, fab_mask, prev, None)

                if fab_prevs[layer_index + 1] is None:
                    fab_prevs[layer_index + 1] = h_t
                else:
                    fab_prevs[layer_index + 1] = torch.cat(
                        [fab_prev[layer_index + 1], h_t],
                        dim=1
                    )

            y_hat_t = self.generator(h_t)

            cnt = 0
            for board in boards:
                if board.is_done() == 0:
                    begin = cnt * beam_size
                    end = begin + beam_size

                    prev_status = {}
                    for layer_index in range(n_dec_layers + 1):
                        prev_status['prev_stae_%d' % layer_index] = fab_prevs[layer_index][begin:end]

                    board.collect_result(y_hat_t[begin:end], prev_status)

                    cnt += 1

            done_cnt = [board.is_done() for board in boards]
            length += 1

        batch_sentences, batch_probs = [], []

        for i, board in enumerate(boards):
            sentences, probs = board.get_n_best(n_best, length_penalty=length_penalty)

            batch_sentences += [sentences]
            batch_probs     += [probs]

        return batch_sentences, batch_probs