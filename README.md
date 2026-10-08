<div align="center">

# Self-Proving Data Integrity for Honeypot Observation

**分散型自律観測基盤による自己証明型データ完全性保証**

2拠点・90日間のハニーポット長期観測で、欠損を隠さず、期間と規模を定量的に開示する。

[![CI](https://github.com/yuzujam/self-proving-observation/actions/workflows/ci.yml/badge.svg)](https://github.com/yuzujam/self-proving-observation/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Preprint DOI](https://img.shields.io/badge/preprint-10.5281%2Fzenodo.23177927-1682d4.svg)](https://doi.org/10.5281/zenodo.23177927)
[![Code DOI](https://img.shields.io/badge/code-10.5281%2Fzenodo.23178244-1682d4.svg)](https://doi.org/10.5281/zenodo.23178244)
![Python: tested on 3.14](https://img.shields.io/badge/python-3.14%20(tested)-3776ab.svg)
![Status: observation completed](https://img.shields.io/badge/status-observation%20completed-lightgrey.svg)

[課題](#何を解いたか) · [設計](#どう解いたか) · [結果](#結果) · [限界](#限界隠さずに書く) · [コード](#このリポジトリの内容) · [English](#english-summary)

</div>

![Observation platform at a glance: 90 days, 99.98% heartbeat coverage, 1 of 20 conditions significant](docs/headline.svg)

## 何を解いたか

既存のハニーポット観測基盤（T-Pot/ELKなど）は、攻撃のスパイクでリソースが枯渇し、観測が途切れる。すると、**「攻撃が少なかった期間」と「観測できなかった期間」を後から区別できず**、データは生存バイアスを含む。

この研究は、観測基盤の**生存性**と、**欠損を隠さない記録**を、攻撃の分析と同じ重さで設計・評価する。

## どう解いたか

- **エッジで集約する。** Suricata → Vector が、1秒ごとの件数・特徴量にメモリ上で集約して中央へ送る。生ログは流さない。
- **全拠点が1分ごとに生存証明（Heartbeat）を送る。** 失敗時は何も送らず、検知は「行の不在」で行う。欠損は解析から除外せず、欠損率として開示する。
- **受付と解析を分離する。** 中央は FastAPI（受付）+ Redis（バッファ）+ ClickHouse。受付が解析に引きずられて止まらない。
- **引き算の設計。** 生ログを転送しない、疎結合、欠損の明示、研究の主張に不要な機能は足さない。

![構成図: エッジで集約して中央へ送り、全拠点が1分ごとにHeartbeatを送る](docs/architecture.svg)

## 結果

2拠点・90日（2026-07-02〜09-30）。

| 項目 | 結果 |
|---|---|
| **Heartbeatカバレッジ**（1分単位） | 中央ノード 99.9853%、エッジノード 99.9830%。見つかった欠損は、除外も補正もせず報告した |
| **欠損率の対照実験**（T-Pot/ELKが基準） | バッチ単位の対応あり解析では、20条件中1条件（ramp、5,000 RPS）のみ補正後に有意。どのバッチ・条件でも提案構成は基準より悪くなかった。効果量は無視できる〜小 |
| **概念ドリフト検知**（Fidelity Guard、LSTM+SHAP） | 「SHAPの変化が精度低下に先行する」という当初仮説は**反証**。代わりに、8ウィンドウ以内に精度低下を確認する指標として機能した（確認検知率 91.2〜96.2%、n=80、合成時系列上） |

![Fidelity Guardの確認検知率（合成時系列、n=80）](docs/fidelity-detection.svg)

## 限界（隠さずに書く）

- Fidelity Guardの結果は、**ドリフトを注入した合成時系列**から得ている。実観測データでの検証は、探索的な1事例のみで、対照でも検知が作動した（特異的ではない）。
- 観測は**2拠点**。一般化の根拠は限られる。
- 事前に手順を固定した周期性分析（曜日効果、7日・24日周期）では、**周期性を確認できなかった**。90日・2拠点・日次の粒度での証拠の不在であり、不在の証拠ではない。
- 90日分の連続したCPU・メモリ・ディスクI/Oの記録は取れておらず、リソース消費の単調増加の有無は確認できなかった。
- 自己証明が及ばない範囲（バックアップ経路で取り込み後に失われた行など）も開示している。

プレプリントのv3では、報告したすべての数値を元データから再計算し、見つかった誤りを訂正した。

## このリポジトリの内容

実運用で使ったコードと設定から、サーバーのアドレス、ホスト名、ユーザー名、認証情報（パスワードのハッシュを含む）を取り除いた**再現性パッケージ**です（Zenodoで公開したコード一式に基づく）。行単位のデータと生のログは含みません。構成と注意点は [`README-package.md`](README-package.md)（英語）を参照してください。

<details>
<summary>ディレクトリ構成</summary>

| パス | 内容 |
|---|---|
| `src/` | 受付（FastAPI）、ワーカー、欠損率計測、周期性分析、統計検定、Fidelity Guard、リソース監視 |
| `proposed/` | 中央ノード構成（Docker Compose、Vector、ClickHouseスキーマ）と、集約ウィンドウのアブレーション実験用の隔離構成 |
| `edge/` | エッジノード構成（Docker Compose、Vector） |
| `baseline/` | 対照実験の測定対象（Elasticsearch/Logstash/Kibana） |
| `scripts/` | 実験、バックアップ、分析スクリプト（事前登録した周期性分析 `analyze_periodicity_final.py` を含む） |
| `tests/` | 単体テスト |
| `docs/` | この README の図 |

</details>

負荷生成スクリプトを、自分が所有しない環境に向けないでください。

`baseline/` は対照実験の測定対象で、認証を無効にしたElasticsearchを含みます。実験用の構成であり、公開環境では使わないでください。

## ライセンス

コード: MIT（[`LICENSE`](LICENSE)）。プレプリント: CC BY 4.0。

## 作者

yuzujam。他の取り組みや連絡先は [プロフィール](https://github.com/yuzujam) を参照してください。

---

## English summary

Long-term honeypot observation suffers from survivorship bias: when the platform itself fails under attack spikes, "few attacks" and "no observation" cannot be told apart afterwards. This project builds an edge-aggregating observation platform in which every site sends a one-minute heartbeat and gaps are recorded and disclosed instead of hidden (self-proving data integrity).

Two sites, 90 days (2026-07-02 to 2026-09-30): one-minute heartbeat coverage 99.9853% (central) and 99.9830% (edge). In the controlled comparison with T-Pot/ELK, analyzed per batch (paired, clustered), 1 of 20 conditions differs significantly after correction, and the proposed configuration was never worse in any batch. The leading-indicator hypothesis for the SHAP-based drift detector was refuted; the detector works as a confirming indicator (synthetic time series only). The periodicity analysis found no weekday effect and no 7- or 24-day cycle. Limitations are listed above and in the preprint.

The code in this repository is the sanitized reproducibility package (addresses, host names, user names and credentials removed). See [`README-package.md`](README-package.md).
