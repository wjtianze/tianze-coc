"""进攻模型的 PPO 训练（多采集进程 + 单训练进程）。

    python -m tianze_coc.training.ppo --envs 127.0.0.1:27650 --home <OwnHomeData> --template <EnemyHomeData> \\
        --out runs/attack --updates 1000

- 采集：每个 `--envs` 地址一个进程（rollout.Worker），用最新参数的 CPU 副本打满一局后回传轨迹。
- 训练：GAE(λ) + PPO 截断目标 + 价值（星数分布交叉熵 + 摧毁率回归 + 回报回归）+ 熵奖励。
  LSTM 状态按整局顺序重算（序列长度 = 局内步数，一般几十到几百步）。
- 记录：每次更新写一行 JSON（episodes.jsonl / updates.jsonl），定期保存检查点，可断点续训。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from ..tensorize import ACTION_TYPES, AttackTensorizer, SPATIAL_CHANNELS
from .models import AttackPolicy, count_params
from .rollout import Curriculum, MatchSampler, Worker, stack

ACTION_KEYS = ("type", "unit", "cell", "count", "wait")


def build_policy(d: int = 192) -> AttackPolicy:
    tz = AttackTensorizer()
    return AttackPolicy(tz.E_DIM, tz.U_DIM, tz.glob_dim(), len(SPATIAL_CHANNELS), len(ACTION_TYPES), d=d)


def to_torch(ob: dict, device) -> dict:
    return {k: torch.as_tensor(v, device=device).float() for k, v in ob.items()}


def worker_main(addr: str, args, param_q: mp.Queue, traj_q: mp.Queue, seed: int) -> None:
    host, port = addr.split(":")
    sampler = MatchSampler(Path(args.template).read_bytes(), Path(args.home).read_bytes(),
                           Curriculum(), random.Random(seed))
    w = Worker(host, int(port), sampler, Path(args.home), Path(args.template))
    policy = build_policy(args.d)
    policy.eval()
    version = -1
    while True:
        while not param_q.empty() or version < 0:
            v, sd, curr = param_q.get()
            policy.load_state_dict(sd)
            sampler.curriculum = curr
            version = v

        def policy_fn(ob, state):
            with torch.no_grad():
                obt = {k: t.unsqueeze(0) for k, t in to_torch(ob, "cpu").items()}
                st = state if state is not None else policy.initial_state(1)
                a, logp, _ent, value, st = policy(obt, st)
                ev = AttackPolicy.expected_stars(value["stars"]).item()
            return {k: int(a[k].item()) for k in ACTION_KEYS}, float(logp.item()), ev, st

        try:
            traj = w.run_episode(policy_fn)
            traj_q.put((version, traj))
        except Exception as e:  # noqa: BLE001  模拟器异常：记录并重连
            traj_q.put((version, {"error": f"{addr}: {type(e).__name__}: {e}"}))
            time.sleep(5)


def gae(rewards, values, gamma=0.997, lam=0.95):
    adv = np.zeros(len(rewards), dtype=np.float32)
    last = 0.0
    for t in reversed(range(len(rewards))):
        nv = values[t + 1] if t + 1 < len(values) else 0.0
        delta = rewards[t] + gamma * nv - values[t]
        last = delta + gamma * lam * last
        adv[t] = last
    return adv, adv + np.asarray(values, dtype=np.float32)


def ppo_update(policy, opt, trajs, device, clip=0.2, epochs=3, ent_coef=0.01, vf_coef=0.5):
    stats = []
    for _ in range(epochs):
        random.shuffle(trajs)
        for tr in trajs:
            ob = to_torch(stack(tr.obs), device)
            act = {k: torch.as_tensor([a[k] for a in tr.actions], device=device) for k in ACTION_KEYS}
            adv, ret = gae(tr.rewards, tr.values)
            adv_t = torch.as_tensor(adv, device=device)
            adv_t = (adv_t - adv_t.mean()) / (adv_t.std() + 1e-6) if len(adv) > 1 else adv_t
            ret_t = torch.as_tensor(ret, device=device)
            old_logp = torch.as_tensor(tr.logp, device=device)
            # 按时间顺序重算 LSTM（一局一个序列）
            state = policy.initial_state(1, device)
            logps, ents, vstars, vdes = [], [], [], []
            T = len(tr.actions)
            for t in range(T):
                o = {k: v[t:t + 1] for k, v in ob.items()}
                a = {k: v[t:t + 1] for k, v in act.items()}
                _, lp, en, val, state = policy(o, state, action=a)
                logps.append(lp)
                ents.append(en)
                vstars.append(val["stars"])
                vdes.append(val["destruction"])
            logp = torch.cat(logps)
            ratio = torch.exp(logp - old_logp)
            pg = -torch.min(ratio * adv_t, torch.clamp(ratio, 1 - clip, 1 + clip) * adv_t).mean()
            vs = torch.cat(vstars)
            final_stars = int(tr.info.get("stars", 0))
            v_ce = F.cross_entropy(vs, torch.full((T,), final_stars, device=device, dtype=torch.long))
            v_ret = F.mse_loss(AttackPolicy.expected_stars(vs), ret_t.clamp(0, 3))
            v_des = F.mse_loss(torch.cat(vdes), torch.full((T,), tr.info.get("destruction", 0) / 100.0, device=device))
            ent = torch.cat(ents).mean()
            loss = pg + vf_coef * (v_ce + v_ret + v_des) - ent_coef * ent
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), 1.0)
            opt.step()
            stats.append({"loss": loss.item(), "pg": pg.item(), "ent": ent.item(),
                          "clipfrac": ((ratio - 1).abs() > clip).float().mean().item()})
    return {k: float(np.mean([s[k] for s in stats])) for k in stats[0]} if stats else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--envs", required=True, help="探针地址列表，逗号分隔：host:port,host:port")
    ap.add_argument("--home", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", default="runs/attack")
    ap.add_argument("--updates", type=int, default=1000)
    ap.add_argument("--episodes-per-update", type=int, default=16)
    ap.add_argument("--d", type=int, default=192)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--resume", default="")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = torch.device(args.device)
    policy = build_policy(args.d).to(device)
    opt = torch.optim.Adam(policy.parameters(), lr=args.lr)
    start = 0
    if args.resume:
        ck = torch.load(args.resume, map_location=device)
        policy.load_state_dict(ck["model"])
        opt.load_state_dict(ck["opt"])
        start = ck["update"] + 1
    print(f"进攻模型参数量 {count_params(policy) / 1e6:.2f}M，设备 {device}")
    ctx = mp.get_context("spawn")
    traj_q = ctx.Queue(maxsize=256)
    param_qs = []
    for i, addr in enumerate(args.envs.split(",")):
        q = ctx.Queue()
        param_qs.append(q)
        ctx.Process(target=worker_main, args=(addr, args, q, traj_q, 1000 + i), daemon=True).start()
    curriculum = Curriculum()

    def broadcast(v):
        sd = {k: t.detach().cpu() for k, t in policy.state_dict().items()}
        for q in param_qs:
            q.put((v, sd, curriculum))

    broadcast(start)
    log = (out / "updates.jsonl").open("a", encoding="utf-8")
    eplog = (out / "episodes.jsonl").open("a", encoding="utf-8")
    for upd in range(start, args.updates):
        trajs = []
        while len(trajs) < args.episodes_per_update:
            v, tr = traj_q.get()
            if isinstance(tr, dict):
                print("采集异常：", tr["error"])
                continue
            if not tr.actions:
                continue
            trajs.append(tr)
            eplog.write(json.dumps({"update": upd, "version": v, **tr.info}, ensure_ascii=False) + "\n")
            eplog.flush()
        st = ppo_update(policy, opt, trajs, device)
        stars = [t.info.get("stars", 0) for t in trajs]
        st.update(update=upd, mean_stars=float(np.mean(stars)), three_star=float(np.mean([s == 3 for s in stars])))
        log.write(json.dumps(st) + "\n")
        log.flush()
        print(st, flush=True)
        broadcast(upd + 1)
        if upd % 20 == 0 or upd == args.updates - 1:
            torch.save({"model": policy.state_dict(), "opt": opt.state_dict(), "update": upd}, out / f"ckpt_{upd:06d}.pt")
            torch.save({"model": policy.state_dict(), "opt": opt.state_dict(), "update": upd}, out / "latest.pt")


if __name__ == "__main__":
    main()
