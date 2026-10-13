"""配置加载：把 config/config.ini 变成带类型的 Settings 对象。

设计要点
--------
* 所有门槛、权重、数据源都在 INI 文件里，代码里不留硬编码阈值。
* 支持 ``${VAR}`` 与 ``${VAR:-默认值}`` 形式引用环境变量，方便放 API key。
* 解析时做范围校验，配置写错会立刻报错，而不是跑出静默的错误结果。
"""

from __future__ import annotations

import os
import re
import configparser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_RELATIVE = Path("config") / "config.ini"


class ConfigError(ValueError):
    """配置文件本身有问题（缺少必填项、数值越界等）。"""


def expand_env(value: str) -> str:
    """展开 ``${VAR}`` / ``${VAR:-default}``；未设置且无默认值时展开为空串。"""

    def _sub(match: "re.Match[str]") -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        return default if default is not None else ""

    return _ENV_PATTERN.sub(_sub, value)


@dataclass
class GeneralConfig:
    project_root: Path
    database: Path
    output_dir: Path
    report_language: str
    market_timezone: str
    display_timezone: str
    benchmark: str
    sector_map_file: Optional[Path]
    catch_up_max_days: int


@dataclass
class UniverseConfig:
    exchanges: List[str]
    security_types: List[str]
    include_adr: bool
    exclude_otc: bool
    min_price: float
    min_market_cap: float
    min_avg_dollar_volume_20d: float
    watchlist_file: Optional[Path]
    max_symbols: int
    prefilter_top_market_cap: int
    missing_market_cap_policy: str
    market_cap_file: Optional[Path]
    exclude_name_keywords: List[str]


@dataclass
class FiltersConfig:
    lookback_days: int
    near_low_pct: float
    min_drawdown_from_high_pct: float
    min_dollar_volume_days: int
    max_stale_days: int
    min_stabilization_score: float
    dip_preference: str
    max_gain_1m_pct: float
    max_gain_3m_pct: float
    max_rsi_for_dip: float
    require_below_ma50: bool
    rebound_action: str
    demote_on_persistent_new_low: bool
    demote_on_extreme_volatility: bool
    earnings_alert_days: int
    volatility_spike_ratio: float
    single_day_move_pct: float
    market_regime_ma: int


@dataclass
class ScoringConfig:
    weight_liquidity: float
    weight_low_position: float
    weight_stabilization: float
    weight_quality: float
    liquidity_volume_share: float
    liquidity_stability_share: float
    liquidity_volume_saturation_x: float
    low_near_low_share: float
    low_drawdown_share: float
    low_decline_share: float
    low_drawdown_saturation_pct: float
    stab_rsi_share: float
    stab_higher_low_share: float
    stab_ma_share: float
    stab_volume_share: float
    quality_enabled: bool
    quality_source: str

    @property
    def total_weight(self) -> float:
        """四个维度的权重之和（默认 100）。"""
        return (
            self.weight_liquidity
            + self.weight_low_position
            + self.weight_stabilization
            + self.weight_quality
        )

    @property
    def technical_max(self) -> float:
        """不含公司质量维度的可用满分（基本面缺失时使用）。"""
        return (
            self.weight_liquidity
            + self.weight_low_position
            + self.weight_stabilization
        )


@dataclass
class DataSourceConfig:
    provider: str
    sample_symbols: int
    sample_days: int
    sample_seed: int
    csv_dir: Optional[Path]
    csv_date_column: str
    alpaca_feed: str
    alpaca_adjustment: str
    alphavantage_outputsize: str
    requests_per_second: float
    max_retries: int
    request_timeout: int
    sec_user_agent: str
    sec_enabled: bool


@dataclass
class NotifyConfig:
    enabled: bool
    channels: List[str]
    min_main_candidates: int
    include_reasons: bool
    smtp_host: str
    smtp_port: int
    smtp_use_tls: bool
    smtp_username: str
    smtp_password: str
    mail_from: str
    mail_to: List[str]
    webhook_url: str
    webhook_kind: str
    telegram_chat_id: str
    macos_notify: bool

    def channel_list(self) -> List[str]:
        return [c for c in self.channels if c]


@dataclass
class OutputConfig:
    max_main_candidates: int
    max_watch_candidates: int
    write_markdown: bool
    write_csv: bool
    write_json: bool
    top_reasons: int
    save_all_features: bool
    write_html: bool
    html_dir: Path


@dataclass
class Settings:
    path: Path
    general: GeneralConfig
    universe: UniverseConfig
    filters: FiltersConfig
    scoring: ScoringConfig
    data_source: DataSourceConfig
    output: OutputConfig
    notify: NotifyConfig
    presets: Dict[str, Dict[str, str]] = field(default_factory=dict)
    default_preset: str = ""
    raw: configparser.ConfigParser = field(repr=False, default=None)

    # -- 便捷属性 ---------------------------------------------------------
    @property
    def project_root(self) -> Path:
        return self.general.project_root


def _split_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _as_bool(raw: str, where: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off", ""):
        return False
    raise ConfigError(f"{where}: 期望布尔值 (true/false)，实际为 {raw!r}")


def _optional_path(raw: str, base: Path) -> Optional[Path]:
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path)


def _path(raw: str, base: Path) -> Path:
    path = Path(raw.strip()).expanduser()
    return path if path.is_absolute() else (base / path)


def load_settings(config_path: Optional[str] = None) -> Settings:
    """读取并校验配置。``config_path`` 为空时按默认位置查找。"""
    path = Path(config_path).expanduser() if config_path else None
    if path is None:
        candidate = Path.cwd() / DEFAULT_CONFIG_RELATIVE
        if not candidate.exists():
            raise ConfigError(
                f"找不到配置文件 {candidate}；请用 --config 指定，或先执行 "
                "`python -m us_low_scan.cli init-db` 查看目录结构。"
            )
        path = candidate
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.optionxform = str  # 保留大小写（ticker 用得上）
    with open(path, "r", encoding="utf-8") as handle:
        parser.read_file(handle)

    for section in (
        "general",
        "universe",
        "filters",
        "scoring",
        "data_source",
        "output",
    ):
        if not parser.has_section(section):
            raise ConfigError(f"配置缺少 [{section}] 段")

    def get(section: str, key: str, default: str = "") -> str:
        if parser.has_option(section, key):
            return expand_env(parser.get(section, key)).strip()
        return default

    def get_float(section: str, key: str, default: float = 0.0) -> float:
        raw = get(section, key)
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"[{section}] {key} 必须是数字，实际为 {raw!r}") from exc

    def get_int(section: str, key: str, default: int = 0) -> int:
        raw = get(section, key)
        if raw == "":
            return default
        try:
            return int(float(raw))
        except ValueError as exc:
            raise ConfigError(f"[{section}] {key} 必须是整数，实际为 {raw!r}") from exc

    root_raw = get("general", "project_root")
    project_root = (
        _path(root_raw, Path.cwd()) if root_raw else path.parent.parent.resolve()
    )

    general = GeneralConfig(
        project_root=project_root,
        database=_path(get("general", "database", "data/market.db"), project_root),
        output_dir=_path(get("general", "output_dir", "reports"), project_root),
        report_language=get("general", "report_language", "zh"),
        market_timezone=get("general", "market_timezone", "America/New_York"),
        display_timezone=get("general", "display_timezone", "Asia/Shanghai"),
        benchmark=get("general", "benchmark", "SPY").upper(),
        sector_map_file=_optional_path(get("general", "sector_map_file"), project_root),
        catch_up_max_days=get_int("general", "catch_up_max_days", 10),
    )

    universe = UniverseConfig(
        exchanges=[e.upper() for e in _split_list(get("universe", "exchanges"))],
        security_types=[s.lower() for s in _split_list(get("universe", "security_types"))],
        include_adr=_as_bool(get("universe", "include_adr", "false"), "[universe] include_adr"),
        exclude_otc=_as_bool(get("universe", "exclude_otc", "true"), "[universe] exclude_otc"),
        min_price=get_float("universe", "min_price", 5.0),
        min_market_cap=get_float("universe", "min_market_cap", 1e9),
        min_avg_dollar_volume_20d=get_float("universe", "min_avg_dollar_volume_20d", 1e7),
        watchlist_file=_optional_path(get("universe", "watchlist_file"), project_root),
        max_symbols=get_int("universe", "max_symbols", 0),
        prefilter_top_market_cap=get_int("universe", "prefilter_top_market_cap", 0),
        missing_market_cap_policy=get("universe", "missing_market_cap_policy", "flag").lower(),
        market_cap_file=_optional_path(get("universe", "market_cap_file"), project_root),
        exclude_name_keywords=_split_list(get("universe", "exclude_name_keywords")),
    )

    filters = FiltersConfig(
        lookback_days=get_int("filters", "lookback_days", 252),
        near_low_pct=get_float("filters", "near_low_pct", 15.0),
        min_drawdown_from_high_pct=get_float("filters", "min_drawdown_from_high_pct", 20.0),
        min_dollar_volume_days=get_int("filters", "min_dollar_volume_days", 20),
        max_stale_days=get_int("filters", "max_stale_days", 7),
        min_stabilization_score=get_float("filters", "min_stabilization_score", 12.0),
        dip_preference=get("filters", "dip_preference", "dip").lower(),
        max_gain_1m_pct=get_float("filters", "max_gain_1m_pct", 10.0),
        max_gain_3m_pct=get_float("filters", "max_gain_3m_pct", 20.0),
        max_rsi_for_dip=get_float("filters", "max_rsi_for_dip", 60.0),
        require_below_ma50=_as_bool(
            get("filters", "require_below_ma50", "false"), "[filters] require_below_ma50"
        ),
        rebound_action=get("filters", "rebound_action", "exclude").lower(),
        demote_on_persistent_new_low=_as_bool(
            get("filters", "demote_on_persistent_new_low", "true"),
            "[filters] demote_on_persistent_new_low",
        ),
        demote_on_extreme_volatility=_as_bool(
            get("filters", "demote_on_extreme_volatility", "true"),
            "[filters] demote_on_extreme_volatility",
        ),
        earnings_alert_days=get_int("filters", "earnings_alert_days", 3),
        volatility_spike_ratio=get_float("filters", "volatility_spike_ratio", 2.5),
        single_day_move_pct=get_float("filters", "single_day_move_pct", 15.0),
        market_regime_ma=get_int("filters", "market_regime_ma", 200),
    )

    scoring = ScoringConfig(
        weight_liquidity=get_float("scoring", "weight_liquidity", 25),
        weight_low_position=get_float("scoring", "weight_low_position", 30),
        weight_stabilization=get_float("scoring", "weight_stabilization", 25),
        weight_quality=get_float("scoring", "weight_quality", 20),
        liquidity_volume_share=get_float("scoring", "liquidity_volume_share", 20),
        liquidity_stability_share=get_float("scoring", "liquidity_stability_share", 5),
        liquidity_volume_saturation_x=get_float(
            "scoring", "liquidity_volume_saturation_x", 10.0
        ),
        low_near_low_share=get_float("scoring", "low_near_low_share", 13),
        low_drawdown_share=get_float("scoring", "low_drawdown_share", 12),
        low_decline_share=get_float("scoring", "low_decline_share", 5),
        low_drawdown_saturation_pct=get_float(
            "scoring", "low_drawdown_saturation_pct", 60.0
        ),
        stab_rsi_share=get_float("scoring", "stab_rsi_share", 8),
        stab_higher_low_share=get_float("scoring", "stab_higher_low_share", 6),
        stab_ma_share=get_float("scoring", "stab_ma_share", 7),
        stab_volume_share=get_float("scoring", "stab_volume_share", 4),
        quality_enabled=_as_bool(
            get("scoring", "quality_enabled", "false"), "[scoring] quality_enabled"
        ),
        quality_source=get("scoring", "quality_source", "none").lower(),
    )

    data_source = DataSourceConfig(
        provider=get("data_source", "provider", "sample").lower(),
        sample_symbols=get_int("data_source", "sample_symbols", 160),
        sample_days=get_int("data_source", "sample_days", 420),
        sample_seed=get_int("data_source", "sample_seed", 20261010),
        csv_dir=_optional_path(get("data_source", "csv_dir"), project_root),
        csv_date_column=get("data_source", "csv_date_column", "date"),
        alpaca_feed=get("data_source", "alpaca_feed", "iex").lower(),
        alpaca_adjustment=get("data_source", "alpaca_adjustment", "split").lower(),
        alphavantage_outputsize=get("data_source", "alphavantage_outputsize", "full").lower(),
        requests_per_second=get_float("data_source", "requests_per_second", 3.0),
        max_retries=get_int("data_source", "max_retries", 3),
        request_timeout=get_int("data_source", "request_timeout", 30),
        sec_user_agent=get(
            "data_source", "sec_user_agent", "us-low-scan research tool"
        ),
        sec_enabled=_as_bool(get("data_source", "sec_enabled", "false"), "[data_source] sec_enabled"),
    )

    output = OutputConfig(
        max_main_candidates=get_int("output", "max_main_candidates", 5),
        max_watch_candidates=get_int("output", "max_watch_candidates", 5),
        write_markdown=_as_bool(get("output", "write_markdown", "true"), "[output] write_markdown"),
        write_csv=_as_bool(get("output", "write_csv", "true"), "[output] write_csv"),
        write_json=_as_bool(get("output", "write_json", "true"), "[output] write_json"),
        top_reasons=get_int("output", "top_reasons", 4),
        save_all_features=_as_bool(
            get("output", "save_all_features", "true"), "[output] save_all_features"
        ),
        write_html=_as_bool(get("output", "write_html", "true"), "[output] write_html"),
        html_dir=_path(get("output", "html_dir", "reports/site"), project_root),
    )

    notify = NotifyConfig(
        enabled=_as_bool(get("notify", "enabled", "false"), "[notify] enabled"),
        channels=[c.lower() for c in _split_list(get("notify", "channels", "email"))],
        min_main_candidates=get_int("notify", "min_main_candidates", 1),
        include_reasons=_as_bool(
            get("notify", "include_reasons", "false"), "[notify] include_reasons"
        ),
        smtp_host=get("notify", "smtp_host"),
        smtp_port=get_int("notify", "smtp_port", 587),
        smtp_use_tls=_as_bool(get("notify", "smtp_use_tls", "true"), "[notify] smtp_use_tls"),
        smtp_username=get("notify", "smtp_username"),
        smtp_password=get("notify", "smtp_password"),
        mail_from=get("notify", "mail_from"),
        mail_to=_split_list(get("notify", "mail_to")),
        webhook_url=get("notify", "webhook_url"),
        webhook_kind=get("notify", "webhook_kind", "generic").lower(),
        telegram_chat_id=get("notify", "telegram_chat_id"),
        macos_notify=_as_bool(get("notify", "macos_notify", "false"), "[notify] macos_notify"),
    )

    presets: Dict[str, Dict[str, str]] = {}
    for section in parser.sections():
        if not section.lower().startswith("preset."):
            continue
        name = section.split(".", 1)[1].strip().lower()
        presets[name] = {
            key: expand_env(parser.get(section, key)).strip() for key in parser.options(section)
        }

    _validate(general, universe, filters, scoring, data_source, output, notify, presets)

    return Settings(
        path=path.resolve(),
        general=general,
        universe=universe,
        filters=filters,
        scoring=scoring,
        data_source=data_source,
        output=output,
        notify=notify,
        presets=presets,
        default_preset=get("presets", "default").lower(),
        raw=parser,
    )


def _validate(general, universe, filters, scoring, data_source, output, notify=None, presets=None) -> None:
    if general.catch_up_max_days < 1:
        raise ConfigError("[general] catch_up_max_days 至少为 1")
    if universe.min_price < 0:
        raise ConfigError("[universe] min_price 不能为负")
    if universe.min_avg_dollar_volume_20d < 0:
        raise ConfigError("[universe] min_avg_dollar_volume_20d 不能为负")
    if universe.missing_market_cap_policy not in ("flag", "exclude"):
        raise ConfigError(
            "[universe] missing_market_cap_policy 只能是 flag 或 exclude，"
            f"实际为 {universe.missing_market_cap_policy!r}"
        )
    if filters.lookback_days < 60:
        raise ConfigError("[filters] lookback_days 至少为 60（需要足够历史计算 52 周区间）")
    if not (0 <= filters.near_low_pct <= 100):
        raise ConfigError("[filters] near_low_pct 应在 0-100 之间")
    if not (0 <= filters.min_drawdown_from_high_pct <= 100):
        raise ConfigError("[filters] min_drawdown_from_high_pct 应在 0-100 之间")
    if filters.min_dollar_volume_days < 5:
        raise ConfigError("[filters] min_dollar_volume_days 至少为 5")
    if filters.market_regime_ma < 20:
        raise ConfigError("[filters] market_regime_ma 至少为 20")
    if filters.dip_preference not in ("dip", "stabilized", "both"):
        raise ConfigError(
            "[filters] dip_preference 只能是 dip / stabilized / both，"
            f"实际为 {filters.dip_preference!r}"
        )
    if filters.rebound_action not in ("exclude", "demote", "off"):
        raise ConfigError(
            "[filters] rebound_action 只能是 exclude / demote / off，"
            f"实际为 {filters.rebound_action!r}"
        )
    if filters.max_rsi_for_dip <= 0 or filters.max_rsi_for_dip > 100:
        raise ConfigError("[filters] max_rsi_for_dip 应在 0-100 之间")
    if scoring.weight_liquidity + scoring.weight_low_position + scoring.weight_stabilization + scoring.weight_quality <= 0:
        raise ConfigError("[scoring] 四个维度权重之和必须大于 0")
    for name, total, parts in (
        (
            "liquidity",
            scoring.weight_liquidity,
            scoring.liquidity_volume_share + scoring.liquidity_stability_share,
        ),
        (
            "low_position",
            scoring.weight_low_position,
            scoring.low_near_low_share + scoring.low_drawdown_share + scoring.low_decline_share,
        ),
        (
            "stabilization",
            scoring.weight_stabilization,
            scoring.stab_rsi_share + scoring.stab_higher_low_share
            + scoring.stab_ma_share + scoring.stab_volume_share,
        ),
    ):
        if abs(total - parts) > 1e-6:
            raise ConfigError(
                f"[scoring] {name} 的分项权重之和 ({parts}) 必须等于维度权重 ({total})"
            )
    if data_source.provider not in ("sample", "csv", "alpaca", "alphavantage", "nasdaq"):
        raise ConfigError(
            "[data_source] provider 只能是 sample / csv / nasdaq / alpaca / alphavantage，"
            f"实际为 {data_source.provider!r}"
        )
    if data_source.provider == "csv" and not data_source.csv_dir:
        raise ConfigError("[data_source] provider=csv 时必须设置 csv_dir")
    if data_source.provider == "alpaca" and not (
        os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")
    ):
        raise ConfigError(
            "[data_source] provider=alpaca 需要环境变量 ALPACA_API_KEY 与 ALPACA_SECRET_KEY"
        )
    if data_source.provider == "alphavantage" and not os.environ.get("ALPHAVANTAGE_API_KEY"):
        raise ConfigError("[data_source] provider=alphavantage 需要环境变量 ALPHAVANTAGE_API_KEY")
    if output.max_main_candidates < 0 or output.max_watch_candidates < 0:
        raise ConfigError("[output] 候选数量不能为负")
    if scoring.quality_enabled and scoring.quality_source == "none":
        raise ConfigError("[scoring] quality_enabled=true 时必须设置 quality_source")

    if notify is not None:
        if notify.enabled:
            for channel in notify.channels:
                if channel not in ("email", "webhook", "macos"):
                    raise ConfigError(
                        f"[notify] 未知渠道 {channel!r}；可选 email / webhook / macos"
                    )
            if "email" in notify.channels and not (notify.smtp_host and notify.mail_to):
                raise ConfigError("[notify] email 渠道需要 smtp_host 与 mail_to")
            if "webhook" in notify.channels:
                if not notify.webhook_url:
                    raise ConfigError("[notify] webhook 渠道需要 webhook_url")
                if notify.webhook_kind not in (
                    "generic", "slack", "wecom", "dingtalk", "telegram"
                ):
                    raise ConfigError(
                        "[notify] webhook_kind 只能是 generic/slack/wecom/dingtalk/telegram"
                    )
                if notify.webhook_kind == "telegram" and not notify.telegram_chat_id:
                    raise ConfigError("[notify] telegram 需要 telegram_chat_id")
    if presets is not None:
        for name, values in presets.items():
            for key in values:
                if key not in PRESET_KEYS:
                    raise ConfigError(
                        f"[preset.{name}] 不认识的键 {key!r}；"
                        f"可用键：{', '.join(sorted(PRESET_KEYS))}"
                    )


# ==========================================================================
# 参数方案（presets）：不改代码、不改数据，只改门槛与权重
# ==========================================================================
from dataclasses import fields as _dc_fields  # noqa: E402

_UNIVERSE_KEYS = {f.name for f in _dc_fields(UniverseConfig)}
_FILTERS_KEYS = {f.name for f in _dc_fields(FiltersConfig)}
_SCORING_KEYS = {f.name for f in _dc_fields(ScoringConfig)}
_OUTPUT_KEYS = {f.name for f in _dc_fields(OutputConfig)}

PRESET_KEYS = _UNIVERSE_KEYS | _FILTERS_KEYS | _SCORING_KEYS | _OUTPUT_KEYS

#: 维度权重 -> 该维度下的分项（改权重时按比例重算分项，保证分项之和仍然等于维度权重）
SHARE_GROUPS = {
    "weight_liquidity": ("liquidity_volume_share", "liquidity_stability_share"),
    "weight_low_position": ("low_near_low_share", "low_drawdown_share", "low_decline_share"),
    "weight_stabilization": (
        "stab_rsi_share",
        "stab_higher_low_share",
        "stab_ma_share",
        "stab_volume_share",
    ),
    "weight_quality": (),
}


def _coerce_like(current, raw: str, where: str):
    if isinstance(current, bool):
        return _as_bool(raw, where)
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    return raw


def rescale_sub_scores(scoring: ScoringConfig, previous: ScoringConfig) -> ScoringConfig:
    """维度权重变化时，把该维度的分项按比例缩放，并让分项之和精确等于新权重。"""
    updates: Dict[str, float] = {}
    for weight_key, share_keys in SHARE_GROUPS.items():
        if not share_keys:
            continue
        old_total = getattr(previous, weight_key)
        new_total = getattr(scoring, weight_key)
        if not old_total or abs(new_total - old_total) < 1e-9:
            continue
        scaled = {}
        for key in share_keys:
            scaled[key] = round(getattr(scoring, key) * (new_total / old_total), 3)
        # 把四舍五入的残差补给占比最大的分项，保证和值精确
        residual = round(new_total - sum(scaled.values()), 3)
        biggest = max(scaled, key=lambda k: scaled[k])
        scaled[biggest] = round(scaled[biggest] + residual, 3)
        updates.update(scaled)
    return replace(scoring, **updates) if updates else scoring


def apply_preset(settings: Settings, name: Optional[str] = None) -> Settings:
    """应用参数方案。``name`` 为空时使用配置里的 ``[presets] default``。"""
    key = (name or settings.default_preset or "").strip().lower()
    if not key or key in ("none", "default"):
        return settings
    if key not in settings.presets:
        available = ", ".join(sorted(settings.presets)) or "（未定义任何方案）"
        raise ConfigError(f"未知的参数方案 {key!r}；可用方案：{available}")

    values = settings.presets[key]
    universe, filters, scoring, output = (
        settings.universe,
        settings.filters,
        settings.scoring,
        settings.output,
    )
    previous_scoring = scoring

    for name_key, raw in values.items():
        where = f"[preset.{key}] {name_key}"
        if name_key in _UNIVERSE_KEYS:
            universe = replace(
                universe, **{name_key: _coerce_like(getattr(universe, name_key), raw, where)}
            )
        elif name_key in _FILTERS_KEYS:
            filters = replace(
                filters, **{name_key: _coerce_like(getattr(filters, name_key), raw, where)}
            )
        elif name_key in _SCORING_KEYS:
            scoring = replace(
                scoring, **{name_key: _coerce_like(getattr(scoring, name_key), raw, where)}
            )
        elif name_key in _OUTPUT_KEYS:
            output = replace(
                output, **{name_key: _coerce_like(getattr(output, name_key), raw, where)}
            )

    scoring = rescale_sub_scores(scoring, previous_scoring)
    updated = replace(
        settings, universe=universe, filters=filters, scoring=scoring, output=output
    )
    validate_settings(updated)
    return updated


def validate_settings(settings: Settings) -> None:
    """对（可能被预设/实验台改过的）配置再跑一次校验。"""
    _validate(
        settings.general,
        settings.universe,
        settings.filters,
        settings.scoring,
        settings.data_source,
        settings.output,
        settings.notify,
        settings.presets,
    )


def preset_names(settings: Settings) -> List[str]:
    return sorted(settings.presets)
