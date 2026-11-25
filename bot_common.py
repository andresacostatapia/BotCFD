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
import sys
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
    "loop_seconds": 60,
    "deviation": 20,                  # desviación máxima en puntos
    "close_positions_on_error": False,
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
