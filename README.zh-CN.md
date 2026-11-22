# skewlens

[English](README.md) | [简体中文](README.zh-CN.md)

**在跑崩你的 Join 之前，先找出问题列。** `skewlens` 用单次并行扫描对 JSONL 数据集做画像分析，围绕真正拖垮大表连接的三件事对每一列打分排序：**热点倾斜键（hot key）、大片空值（nulls）、以及内存放不下的超高基数（cardinality）**。

```console
$ skewlens run --rows 1000000 --workdir demo
skewlens 0.1.0  1,000,000 rows  112.3 MB  0.8409 s  133.5 MB/s  4 worker(s)
 #  column           score  grade     top share   nulls
 1  user_id           0.55  critical      13.9%    0.0%
 2  referrer          0.27  medium         1.3%   84.9%
 3  session_id        0.12  low            0.0%    0.0%
 4  country           0.09  low            2.5%    0.0%
 5  amount            0.00  low            0.0%    0.0%
report: demo/report.md
```

## 操作工作流与流程图

```mermaid
flowchart LR
    A["1. 准备或合成 JSONL 数据<br/>(skewlens synth 或自有 events.jsonl)"] --> B["2. 多线程并行草图扫描<br/>(scanner/scan.ts: HLL + Count-Min + Chan)"]
    B --> C["3. 综合倾斜/空值/基数/漂移评分<br/>(skewlens report -> report.md)"]
    C --> D["4. 识别高危 Join 热点列并处理"]
```

## 核心算法与内存恒定特性

不把整列物化到内存，而是使用大小与行数无关的流式草图：

- **HyperLogLog**（$2^{14}$ 寄存器，标准误差约 0.8%）估算基数；唯一值少于 2,000 时保留精确集合。
- **Count-Min Sketch**（4,096 × 4）配合 Space-Saving 候选集追踪高频热点键。
- **Chan 并行方差公式**计算数值列的均值与标准差。

实测（Windows 11，12 逻辑核，100 万行 112.3 MB JSONL）：4 线程耗时 **0.82 秒**（137.3 MB/s），8 线程 **0.67 秒**（166.9 MB/s）；从 10 万行扩到 100 万行，JS 堆内存保持在 **20–34 MB**，不随行数增长。

## 安装与常用工作流命令

环境要求：Python 3.11+、Node 22.6+（通过 `--experimental-strip-types` 直接运行 TypeScript，零 `node_modules`）。

```bash
python -m pip install -e .

# 一键生成 100 万行数据、扫描并出报告
skewlens run --rows 1000000 --workdir demo --workers 4
```

针对自有 JSONL 数据集：

```bash
skewlens scan --input events.jsonl --workers 8 --out profile.json
skewlens report --profile profile.json --out report.md
```

## 评分权重与测试

综合风险分权重：`skew (0.55)` + `nulls (0.25)` + `cardinality (0.12)` + `drift (0.08)`，等级分为 `critical (≥0.55)`、`high (≥0.33)`、`medium (≥0.14)`、`low`。

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -t tests -v
ruff check . && mypy
```

## 开源协议

MIT，详见 [LICENSE](LICENSE)。
