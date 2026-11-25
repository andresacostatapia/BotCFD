"""
bot_common.py
=============
Utilidades compartidas por todos los módulos del bot:

* Rutas del proyecto (relativas a este archivo, sin hardcoding).
* Configuración centralizada en ``risk_config.json`` (carga, validación, guardado).
* Logging estructurado con archivo rotativo en ``logs/``.

Este módulo NO tiene efectos secundarios al importarse: no crea carpetas,
no abre archivos y no configura logging hasta que se llama a sus funciones.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Rutas (constantes, sin efectos secundarios)
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
CONFIG_PATH: Path = BASE_DIR / "risk_config.json"
LOG_DIR: Path = BASE_DIR / "logs"
EVOLVED_PARAMS_PATH: Path = BASE_DIR / "evolved_params.json"

LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

VALID_TIMEFRAMES: tuple[str, ...] = ("M1", "M5", "M15", "H1", "H4")
VALID_STRATEGIES: tuple[str, ...] = ("cruce_medias", "reversion_media", "ruptura")

# ---------------------------------------------------------------------------
# Perfiles (varias instancias del bot, cada una con su risk_config_<nombre>.json)
# ---------------------------------------------------------------------------
MAIN_PROFILE: str = "principal"
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


@dataclass(frozen=True)
class ProfilePaths:
    """Archivos asociados a un perfil (todos relativos a la carpeta de su configuración)."""

    name: str
    config: Path
    evolved: Path      # resultado de la última evolución
    history: Path      # historial de evoluciones (una línea JSON por ejecución)
    status: Path       # estado en vivo que escribe el bot para el Centro de control
    stop_flag: Path    # si existe, el bot se detiene de forma ordenada
    log_file: str      # nombre del log rotativo dentro de logs/


def profile_name(config_path: Path) -> str:
    """'risk_config.json' -> 'principal'; 'risk_config_m5.json' -> 'm5'."""
    stem = Path(config_path).stem
    if stem == "risk_config":
        return MAIN_PROFILE
    if stem.startswith("risk_config_"):
        return stem[len("risk_config_"):]
    return stem


def config_path_for(name: str, base_dir: Path | None = None) -> Path:
    """Ruta del archivo de configuración de un perfil."""
    base = base_dir or BASE_DIR
    return base / ("risk_config.json" if name == MAIN_PROFILE else f"risk_config_{name}.json")


def profile_paths(config_path: Path) -> ProfilePaths:
    """Calcula los archivos de un perfil a partir de su archivo de configuración."""
    config_path = Path(config_path)
    name = profile_name(config_path)
    suffix = "" if name == MAIN_PROFILE else f"_{name}"
    logs = config_path.parent / "logs"
    return ProfilePaths(
        name=name,
        config=config_path,
        evolved=config_path.parent / f"evolved_params{suffix}.json",
        history=logs / f"evolution_history_{name}.jsonl",
        status=logs / f"status_{name}.json",
        stop_flag=logs / f"stop_{name}.flag",
        log_file=f"trading_bot{suffix}.log",
    )


def list_profiles(base_dir: Path | None = None) -> list[str]:
    """Perfiles existentes: 'principal' primero y luego risk_config_*.json ordenados."""
    base = base_dir or BASE_DIR
    names = [MAIN_PROFILE] if (base / "risk_config.json").exists() else []
    names += sorted(
        profile_name(p) for p in base.glob("risk_config_*.json")
        if PROFILE_NAME_RE.match(profile_name(p))
    )
    return names


def write_json_atomic(path: Path, data: Any) -> bool:
    """Escribe JSON de forma atómica. Devuelve False si falla (p. ej. archivo bloqueado)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def read_json(path: Path) -> Any | None:
    """Lee un JSON; None si no existe o no se puede leer."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def now_iso() -> str:
    """Fecha/hora local en formato ISO (segundos)."""
    return datetime.now().isoformat(timespec="seconds")

# ---------------------------------------------------------------------------
# Configuración por defecto
# ---------------------------------------------------------------------------
# Las claves "GUI" se editan desde config_panel.py; las "avanzadas" se pueden
# editar a mano en risk_config.json y la GUI las conserva al guardar.
DEFAULT_CONFIG: dict[str, Any] = {
    # --- Editables desde la GUI ---
    "risk_percent": 1.0,              # % del equity arriesgado por operación
    "max_daily_loss_percent": 3.0,    # % de pérdida diaria que detiene el trading
    "max_lot": 1.0,
    "min_lot": 0.01,
    "max_spread": 0.00020,            # spread máximo en unidades de precio
    "magic": 20250101,
    "symbol": "EURUSD",
    "timeframe": "H1",
    "require_demo": True,
    # --- Avanzadas: evolución / backtest ---
    "history_bars": 2000,             # velas históricas para evolucionar
    "population": 20,
    "generations": 10,
    "evolver_backend": "auto",        # "auto" | "strategy-genome" | "deap"
    "evolver_seed": None,             # int para reproducibilidad, o null
    "commission_per_trade": 0.00007,  # coste fijo estimado por trade (precio)
    "min_trades": 20,                 # mínimo de trades in-sample sin penalizar
    "min_trades_oos": 5,              # mínimo de trades out-of-sample
    "fitness_folds": 3,               # tramos del entrenamiento; fitness = peor tramo (1 = desactivado)
    "train_ratio": 0.70,              # walk-forward: 70% train / 30% validación
    "oos_pf_ratio_min": 0.80,         # PF_oos debe ser >= 0.8 * PF_is
    "min_pf_in_sample": 1.0,          # PF in-sample mínimo aceptable
    "pf_cap": 10.0,                   # tope de PF (evita infinito sin pérdidas)
    # --- Avanzadas: ejecución ---
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 2.0,
    "evolve_sl_tp": True,             # evolucionar también los multiplicadores SL/TP
    "strategy": "cruce_medias",       # cruce_medias | reversion_media | ruptura
    "loop_seconds": 60,
    "heartbeat_seconds": 60,          # mensaje "En espera" cada N segundos (0 = desactivado)
    "deviation": 20,                  # desviación máxima en puntos
    "close_positions_on_error": False,
    "max_open_positions_total": 3,    # máx. posiciones abiertas entre TODOS los perfiles (0 = sin límite)
    "daily_loss_scope": "bots",       # "bots" = solo operaciones de los bots | "cuenta" = equity de toda la cuenta
    "terminal_path": "",              # ruta a terminal64.exe (opcional)
}


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
def validate_config(config: dict[str, Any]) -> list[str]:
    """Valida la configuración y devuelve una lista de errores (vacía si es válida)."""
    errors: list[str] = []

    def _num(key: str) -> float | None:
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"'{key}' debe ser numérico (valor actual: {value!r})")
            return None
        return float(value)

    risk = _num("risk_percent")
    if risk is not None and not 0 < risk <= 10:
        errors.append("'risk_percent' debe estar entre 0 y 10 (%)")

    daily = _num("max_daily_loss_percent")
    if daily is not None and not 0 < daily <= 50:
        errors.append("'max_daily_loss_percent' debe estar entre 0 y 50 (%)")

    min_lot = _num("min_lot")
    max_lot = _num("max_lot")
    if min_lot is not None and min_lot <= 0:
        errors.append("'min_lot' debe ser > 0")
    if max_lot is not None and max_lot <= 0:
        errors.append("'max_lot' debe ser > 0")
    if min_lot is not None and max_lot is not None and min_lot > max_lot:
        errors.append("'min_lot' no puede ser mayor que 'max_lot'")

    spread = _num("max_spread")
    if spread is not None and spread <= 0:
        errors.append("'max_spread' debe ser > 0")

    magic = config.get("magic")
    if isinstance(magic, bool) or not isinstance(magic, int) or magic <= 0:
        errors.append("'magic' debe ser un entero positivo")

    symbol = config.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        errors.append("'symbol' no puede estar vacío")

    if config.get("timeframe") not in VALID_TIMEFRAMES:
        errors.append(f"'timeframe' debe ser uno de {VALID_TIMEFRAMES}")

    if config.get("strategy") not in VALID_STRATEGIES:
        errors.append(f"'strategy' debe ser una de {VALID_STRATEGIES}")

    if not isinstance(config.get("evolve_sl_tp"), bool):
        errors.append("'evolve_sl_tp' debe ser true/false")

    if not isinstance(config.get("require_demo"), bool):
        errors.append("'require_demo' debe ser true/false")

    ratio = _num("train_ratio")
    if ratio is not None and not 0.5 <= ratio <= 0.9:
        errors.append("'train_ratio' debe estar entre 0.5 y 0.9")

    for key in ("history_bars", "population", "generations", "min_trades",
                "atr_period", "loop_seconds", "deviation"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"'{key}' debe ser un entero positivo")

    folds = config.get("fitness_folds")
    if isinstance(folds, bool) or not isinstance(folds, int) or not 1 <= folds <= 10:
        errors.append("'fitness_folds' debe ser un entero entre 1 y 10")

    if config.get("daily_loss_scope") not in ("cuenta", "bots"):
        errors.append("'daily_loss_scope' debe ser 'cuenta' o 'bots'")

    mot = config.get("max_open_positions_total")
    if isinstance(mot, bool) or not isinstance(mot, int) or mot < 0:
        errors.append("'max_open_positions_total' debe ser un entero >= 0 (0 = sin límite)")

    hb = config.get("heartbeat_seconds")
    if isinstance(hb, bool) or not isinstance(hb, int) or hb < 0:
        errors.append("'heartbeat_seconds' debe ser un entero >= 0 (0 = desactivado)")

    for key in ("sl_atr_mult", "tp_atr_mult", "oos_pf_ratio_min", "pf_cap"):
        value = _num(key)
        if value is not None and value <= 0:
            errors.append(f"'{key}' debe ser > 0")

    return errors


def load_config(path: Path | None = None, *, must_exist: bool = True) -> dict[str, Any]:
    """
    Carga ``risk_config.json`` y lo combina con los valores por defecto.

    :param path: ruta del archivo (por defecto ``CONFIG_PATH``).
    :param must_exist: si es True y el archivo no existe, lanza FileNotFoundError;
                       si es False devuelve una copia de ``DEFAULT_CONFIG``.
    :raises ValueError: si el JSON es inválido o la configuración no valida.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        if must_exist:
            raise FileNotFoundError(
                f"No existe el archivo de configuración: {path}. "
                "Ejecute primero config_panel.py y pulse 'Guardar configuración'."
            )
        return dict(DEFAULT_CONFIG)

    try:
        with path.open("r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido en {path}: {exc}") from exc

    if not isinstance(user_cfg, dict):
        raise ValueError(f"{path} debe contener un objeto JSON")

    config = {**DEFAULT_CONFIG, **user_cfg}
    errors = validate_config(config)
    if errors:
        raise ValueError("Configuración inválida:\n  - " + "\n  - ".join(errors))
    return config


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    """
    Valida y guarda la configuración de forma atómica (archivo temporal + replace).

    :returns: la ruta escrita.
    :raises ValueError: si la configuración no es válida.
    """
    path = path or CONFIG_PATH
    merged = {**DEFAULT_CONFIG, **config}
    errors = validate_config(merged)
    if errors:
        raise ValueError("Configuración inválida:\n  - " + "\n  - ".join(errors))

    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=4, ensure_ascii=False)
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(
    log_file: str = "bot.log",
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """
    Configura el logger raíz con consola (stdout) + archivo rotativo en logs/.

    Es idempotente: llamarla varias veces no duplica handlers.
    Se usa stdout (no stderr) para que PowerShell no trate los logs como errores.

    :returns: el logger raíz configurado.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    if getattr(root, "_mt5bot_configured", False):
        return root

    formatter = logging.Formatter(LOG_FORMAT)

    if sys.stdout is not None:  # con pythonw.exe no hay consola
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_DIR / log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root._mt5bot_configured = True  # type: ignore[attr-defined]
    return root
