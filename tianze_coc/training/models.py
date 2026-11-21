"""三个模型的网络结构（PyTorch）。设计见 docs/model_plan.md 第 2～5 节。

- `SharedEncoder`：实体 Transformer + 空间卷积 + 全局 MLP，三个模型共用的输入表示。
- `AttackPolicy`：进攻模型。LSTM 记忆；分层自回归动作头（类型 → 单位指针 → 位置热力图 → 数量/等待），
  非法选项一律屏蔽；价值头输出星数分布 P(0/1/2/3 星) 和摧毁率；另有开战前头（攻城机器指针 + 大守护者模式）。
- `SetGenerator`：配兵 / 摆阵都可以看成"逐步往集合里加东西"的自回归生成器（指针网络 + 可选位置热力图），
  用于配兵模型和摆阵模型的建筑放置阶段。
参数量约 3～8M（取决于 d_model），单卡即可训练。
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

NEG = -1e9


def masked_logits(logits: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    return logits.masked_fill(mask <= 0, NEG)


class SharedEncoder(nn.Module):
    def __init__(self, ent_dim: int, glob_dim: int, spatial_ch: int, d: int = 192, layers: int = 3, heads: int = 6):
        super().__init__()
        self.ent_in = nn.Linear(ent_dim, d)
        enc_layer = nn.TransformerEncoderLayer(d, heads, 4 * d, dropout=0.0, batch_first=True, norm_first=True)
        self.ent = nn.TransformerEncoder(enc_layer, layers)
        self.glob = nn.Sequential(nn.Linear(glob_dim, d), nn.ReLU(), nn.Linear(d, d))
        self.conv = nn.Sequential(
            nn.Conv2d(spatial_ch, 32, 3, padding=1), nn.ReLU(),
            nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(),
            nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(),
        )
        self.spatial_pool = nn.Linear(64, d)
        self.d = d

    def forward(self, entities, entity_mask, spatial, glob):
        x = self.ent_in(entities)
        pad = entity_mask <= 0
        # 全部被屏蔽时 Transformer 会产生 NaN：至少保留第一个位置
        pad[:, 0] = False
        x = self.ent(x, src_key_padding_mask=pad)
        m = entity_mask.unsqueeze(-1)
        ent_pool = (x * m).sum(1) / m.sum(1).clamp(min=1)
        fmap = self.conv(spatial)  # [B, 64, 50, 50]
        sp_pool = self.spatial_pool(fmap.mean((2, 3)))
        g = self.glob(glob)
        return x, fmap, torch.cat([ent_pool, sp_pool, g], -1)


class AttackPolicy(nn.Module):
    def __init__(self, ent_dim: int, unit_dim: int, glob_dim: int, spatial_ch: int, n_types: int,
                 n_counts: int = 4, n_waits: int = 4, d: int = 192, spell_types: tuple[int, ...] = (3, 7)):
        super().__init__()
        self.register_buffer("spell_types", torch.tensor(spell_types, dtype=torch.long), persistent=False)
        self.enc = SharedEncoder(ent_dim, glob_dim, spatial_ch, d)
        self.unit_in = nn.Sequential(nn.Linear(unit_dim, d), nn.ReLU(), nn.Linear(d, d))
        self.core = nn.LSTMCell(3 * d + d, 2 * d)  # 输入 = 编码器汇总 + 可用单位汇总
        h = 2 * d
        self.type_head = nn.Linear(h, n_types)
        self.unit_query = nn.Linear(h + n_types, d)
        self.loc_film = nn.Linear(h + d, 2 * 64)  # 用核心状态 + 所选单位调制空间特征图
        self.loc_head = nn.Conv2d(64, 1, 1)
        self.count_head = nn.Linear(h + d, n_counts)
        self.wait_head = nn.Linear(h, n_waits)
        self.value_stars = nn.Linear(h, 4)
        self.value_destr = nn.Linear(h, 1)
        # 开战前：攻城机器指针 + 大守护者模式
        self.pre_query = nn.Linear(3 * d, d)
        self.pre_mode = nn.Linear(3 * d, 2)
        self.n_types = n_types

    def initial_state(self, batch: int, device=None):
        h = self.core.hidden_size
        z = torch.zeros(batch, h, device=device)
        return (z, z.clone())

    def encode(self, ob, state):
        ent_x, fmap, summary = self.enc(ob["entities"], ob["entity_mask"], ob["spatial"], ob["glob"])
        u = self.unit_in(ob["units"])
        um = ob["unit_mask"].unsqueeze(-1)
        u_pool = (u * um).sum(1) / um.sum(1).clamp(min=1)
        hx, cx = self.core(torch.cat([summary, u_pool], -1), state)
        return hx, cx, u, fmap, summary

    def forward(self, ob, state, action=None):
        """action=None 时采样；否则计算给定动作的对数概率与熵（PPO 更新用）。
        action / 返回的动作为 dict：type, unit, cell, count, wait（LongTensor[B]）。"""
        hx, cx, u, fmap, _ = self.encode(ob, state)
        B = hx.shape[0]
        out_a, logp, ent = {}, 0.0, 0.0

        def pick(name, logits):
            nonlocal logp, ent
            dist = torch.distributions.Categorical(logits=logits)
            a = dist.sample() if action is None else action[name]
            out_a[name] = a
            logp = logp + dist.log_prob(a)
            ent = ent + dist.entropy()
            return a

        t_logits = masked_logits(self.type_head(hx), ob["type_mask"])
        t = pick("type", t_logits)
        t1 = F.one_hot(t, self.n_types).float()
        q = self.unit_query(torch.cat([hx, t1], -1))
        u_logits = (u @ q.unsqueeze(-1)).squeeze(-1) / (u.shape[-1] ** 0.5)
        # 单位必须与所选动作类型一致：unit_type_mask [B, A, U]
        utm = ob["unit_type_mask"].gather(1, t.view(B, 1, 1).expand(-1, 1, u.shape[1])).squeeze(1)
        um = ob["unit_mask"] * utm
        has_unit = (um.sum(-1) > 0).float()
        um = torch.where(has_unit.unsqueeze(-1) > 0, um, torch.ones_like(um))
        ui = pick("unit", masked_logits(u_logits, um))
        sel = u.gather(1, ui.view(B, 1, 1).expand(-1, 1, u.shape[-1])).squeeze(1)
        film = self.loc_film(torch.cat([hx, sel], -1)).view(B, 2, 64, 1, 1)
        loc = self.loc_head(F.relu(fmap * (1 + film[:, 0]) + film[:, 1])).flatten(1)  # [B, 2500]
        # 法术可以放在地图内任意格；其余部署只能放在可部署格子上
        is_spell = (t.unsqueeze(-1) == self.spell_types.unsqueeze(0)).any(-1, keepdim=True)
        lm = torch.where(is_spell, torch.ones_like(ob["deploy_mask"]), ob["deploy_mask"])
        pick("cell", masked_logits(loc, lm))
        pick("count", self.count_head(torch.cat([hx, sel], -1)))
        pick("wait", self.wait_head(hx))
        value = {"stars": self.value_stars(hx), "destruction": torch.sigmoid(self.value_destr(hx)).squeeze(-1)}
        return out_a, logp, ent, value, (hx, cx)

    @staticmethod
    def expected_stars(value_logits: torch.Tensor) -> torch.Tensor:
        p = F.softmax(value_logits, -1)
        return (p * torch.arange(4, device=p.device, dtype=p.dtype)).sum(-1)

    def pre_battle(self, ob, siege_mask, sample: bool = True):
        """开战前决策：返回 (攻城机器下标, 大守护者模式)。siege_mask 对齐 ob['units']。"""
        _, _, summary = self.enc(ob["entities"], ob["entity_mask"], ob["spatial"], ob["glob"])
        u = self.unit_in(ob["units"])
        q = self.pre_query(summary)
        logits = masked_logits((u @ q.unsqueeze(-1)).squeeze(-1), siege_mask)
        mode_logits = self.pre_mode(summary)
        if sample:
            return (torch.distributions.Categorical(logits=logits).sample(),
                    torch.distributions.Categorical(logits=mode_logits).sample())
        return logits.argmax(-1), mode_logits.argmax(-1)


class SetGenerator(nn.Module):
    """自回归集合生成器（配兵 / 摆阵放置）。

    输入：候选项 tokens [B, N, D]（例如所有可用单位，或所有待放建筑）、上下文向量 [B, C]（账号、模式、预期对手、约束）、
    以及当前已生成的集合摘要。每一步输出：选哪一个候选（指针，屏蔽不可选的）+ 可选的位置热力图（摆阵用）。
    "结束"作为一个额外的候选（下标 N）。
    """

    def __init__(self, item_dim: int, ctx_dim: int, d: int = 192, with_location: bool = False, grid_ch: int = 0):
        super().__init__()
        self.item_in = nn.Sequential(nn.Linear(item_dim, d), nn.ReLU(), nn.Linear(d, d))
        self.ctx_in = nn.Sequential(nn.Linear(ctx_dim, d), nn.ReLU(), nn.Linear(d, d))
        self.end_token = nn.Parameter(torch.zeros(d))
        self.rnn = nn.GRUCell(2 * d, d)
        self.query = nn.Linear(d, d)
        self.with_location = with_location
        if with_location:
            self.grid = nn.Sequential(nn.Conv2d(grid_ch, 32, 3, padding=1), nn.ReLU(), nn.Conv2d(32, 64, 3, padding=1),
                                      nn.ReLU())
            self.loc_film = nn.Linear(2 * d, 2 * 64)
            self.loc_head = nn.Conv2d(64, 1, 1)
        self.value = nn.Linear(d, 1)

    def start(self, ctx):
        return self.ctx_in(ctx)

    def step(self, h, items, item_mask, last_item_emb, grid=None, loc_mask=None, action=None):
        """一步：返回 (选中下标, 位置或 None, logp, 熵, 新状态, 价值)。item_mask [B, N+1]（最后一位是"结束"）。"""
        B = items.shape[0]
        e = self.item_in(items)
        e = torch.cat([e, self.end_token.expand(B, 1, -1)], 1)
        h = self.rnn(torch.cat([last_item_emb, h], -1), h)
        q = self.query(h)
        logits = masked_logits((e @ q.unsqueeze(-1)).squeeze(-1) / (e.shape[-1] ** 0.5), item_mask)
        d1 = torch.distributions.Categorical(logits=logits)
        i = d1.sample() if action is None else action["item"]
        logp, ent = d1.log_prob(i), d1.entropy()
        cell = None
        sel = e.gather(1, i.view(B, 1, 1).expand(-1, 1, e.shape[-1])).squeeze(1)
        if self.with_location and grid is not None:
            fmap = self.grid(grid)
            film = self.loc_film(torch.cat([h, sel], -1)).view(B, 2, 64, 1, 1)
            loc = self.loc_head(F.relu(fmap * (1 + film[:, 0]) + film[:, 1])).flatten(1)
            d2 = torch.distributions.Categorical(logits=masked_logits(loc, loc_mask))
            cell = d2.sample() if action is None else action["cell"]
            is_end = (i == items.shape[1]).float()
            logp = logp + d2.log_prob(cell) * (1 - is_end)
            ent = ent + d2.entropy() * (1 - is_end)
        return i, cell, logp, ent, h, sel, self.value(h).squeeze(-1)


def count_params(m: nn.Module) -> int:
    return sum(p.numel() for p in m.parameters())
