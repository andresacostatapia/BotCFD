"""
config_panel.py
===============
Panel gráfico (tkinter) para editar ``risk_config.json`` y probar la
conexión con MetaTrader 5.

Uso:
    python config_panel.py

Al cerrar la ventana el script termina (start_bot.bat espera a que se cierre
antes de lanzar trading_bot.py).
"""

from __future__ import annotations

import logging
import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from bot_common import (
    BASE_DIR, CONFIG_PATH, DEFAULT_CONFIG, VALID_TIMEFRAMES, load_config, save_global_config,
    save_profile, setup_logging,
)

logger = logging.getLogger("config_panel")

# (clave, etiqueta, tipo) de los campos numéricos/texto
FIELDS: list[tuple[str, str, type]] = [
    ("risk_percent", "Riesgo por operación (%)", float),
    ("max_daily_loss_percent", "Pérdida diaria máxima (%)", float),
    ("max_lot", "Lotaje máximo", float),
    ("min_lot", "Lotaje mínimo", float),
    ("max_spread", "Spread máximo permitido (precio)", float),
    ("magic", "Magic number", int),
    ("symbol", "Símbolo (Entry)", str),
]

COLOR_OK = "#1b7f2a"
COLOR_ERR = "#c62828"
COLOR_INFO = "#455a64"

_FLOAT_RE = re.compile(r"^\d*\.?\d*$")
_INT_RE = re.compile(r"^\d*$")


class ConfigPanel(tk.Tk):
    """Ventana principal del panel de configuración."""

    def __init__(self) -> None:
        """Construye la ventana y carga la configuración existente (o la de defecto)."""
        super().__init__()
        self.title("Panel de Configuración MT5 Bot")
        self.geometry("450x540")
        self.minsize(420, 480)

        self.vars: dict[str, tk.StringVar] = {}
        self.timeframe_var = tk.StringVar()
        self.demo_var = tk.BooleanVar()
        self.status_var = tk.StringVar(value="Listo.")
        self.base_config: dict[str, Any] = dict(DEFAULT_CONFIG)

        self._build_widgets()
        self._load_into_form()

    # ----------------------------------------------------------------- widgets
    def _validator(self, pattern: re.Pattern[str]) -> tuple[str, str]:
        """Crea un validatecommand de tkinter que sólo acepta el patrón dado."""
        def cmd(proposed: str) -> bool:
            return bool(pattern.match(proposed))

        return (self.register(cmd), "%P")

    def _build_widgets(self) -> None:
        """Crea todos los controles de la ventana."""
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Configuración de riesgo", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        float_vcmd = self._validator(_FLOAT_RE)
        int_vcmd = self._validator(_INT_RE)

        row = 1
        for key, label, kind in FIELDS:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar()
            self.vars[key] = var
            if kind is float:
                entry = ttk.Entry(frame, textvariable=var, validate="key", validatecommand=float_vcmd)
            elif kind is int:
                entry = ttk.Entry(frame, textvariable=var, validate="key", validatecommand=int_vcmd)
            else:
                entry = ttk.Entry(frame, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", pady=4, padx=(10, 0))
            row += 1

        ttk.Label(frame, text="Timeframe").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame, textvariable=self.timeframe_var, values=list(VALID_TIMEFRAMES), state="readonly"
        ).grid(row=row, column=1, sticky="ew", pady=4, padx=(10, 0))
        row += 1

        ttk.Checkbutton(frame, text="Solo cuenta DEMO", variable=self.demo_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 4)
        )
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 8))
        buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(buttons, text="Guardar configuración", command=self.on_save).grid(
            row=0, column=0, sticky="ew", padx=(0, 5)
        )
        ttk.Button(buttons, text="Probar conexión MT5", command=self.on_test_connection).grid(
            row=0, column=1, sticky="ew", padx=(5, 0)
        )
        row += 1

        self.status_label = tk.Label(
            frame, textvariable=self.status_var, fg=COLOR_INFO, wraplength=400,
            justify="left", anchor="w",
        )
        self.status_label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    # ------------------------------------------------------------------ estado
    def set_status(self, text: str, color: str = COLOR_INFO) -> None:
        """Actualiza el label de estado con el color indicado (verde/rojo/gris)."""
        self.status_var.set(text)
        self.status_label.configure(fg=color)
        self.update_idletasks()

    # ------------------------------------------------------------ formulario
    def _load_into_form(self) -> None:
        """Rellena el formulario con risk_config.json o con los valores por defecto."""
        try:
            self.base_config = load_config(CONFIG_PATH, must_exist=False)
            origin = CONFIG_PATH.name if CONFIG_PATH.exists() else "valores por defecto"
            self.set_status(f"Configuración cargada desde {origin}.")
        except ValueError as exc:
            self.base_config = dict(DEFAULT_CONFIG)
            self.set_status(f"risk_config.json inválido, se usan valores por defecto: {exc}", COLOR_ERR)

        for key, _label, kind in FIELDS:
            value = self.base_config[key]
            self.vars[key].set(f"{value:.5f}".rstrip("0").rstrip(".") if kind is float else str(value))
        self.timeframe_var.set(self.base_config["timeframe"])
        self.demo_var.set(bool(self.base_config["require_demo"]))

    def collect(self) -> dict[str, Any]:
        """
        Lee y convierte los valores del formulario.

        :raises ValueError: si algún campo está vacío o no es convertible.
        """
        data: dict[str, Any] = dict(self.base_config)  # conserva claves avanzadas
        for key, label, kind in FIELDS:
            raw = self.vars[key].get().strip()
            if not raw:
                raise ValueError(f"El campo '{label}' está vacío")
            try:
                data[key] = kind(raw)  # el símbolo respeta mayúsculas (sufijos .m, .r...)
            except ValueError as exc:
                raise ValueError(f"Valor inválido en '{label}': {raw!r}") from exc
        data["timeframe"] = self.timeframe_var.get()
        data["require_demo"] = bool(self.demo_var.get())
        return data

    # ----------------------------------------------------------------- acciones
    def on_save(self) -> None:
        """Valida y guarda risk_config.json; muestra confirmación."""
        try:
            data = self.collect()
            # Lo común va a global_config.json y lo propio del símbolo a risk_config.json.
            save_global_config(data, BASE_DIR)
            path = save_profile(data, CONFIG_PATH)
        except ValueError as exc:
            self.set_status(f"Error: {exc}", COLOR_ERR)
            messagebox.showerror("Configuración inválida", str(exc), parent=self)
            return
        except OSError as exc:
            self.set_status(f"No se pudo escribir el archivo: {exc}", COLOR_ERR)
            messagebox.showerror("Error de escritura", str(exc), parent=self)
            return

        self.base_config = data
        logger.info("Configuración guardada en %s", path)
        self.set_status(f"Configuración guardada en {path.name}.", COLOR_OK)
        messagebox.showinfo("Configuración guardada", f"Se guardó correctamente:\n{path}", parent=self)

    def _check_symbol(self, mt5c: Any, symbol: str) -> tuple[bool, str]:
        """
        Verifica el símbolo en el bróker (ofrece corregir el nombre si no existe)
        y compara su spread actual con el spread máximo del formulario.

        :returns: (todo correcto, texto para el label de estado).
        """
        if not mt5c.ensure_symbol(symbol):
            suggestions = mt5c.find_similar_symbols(symbol)
            if not suggestions:
                return False, (f"El símbolo '{symbol}' no existe en este bróker. Copie el nombre "
                               "exacto desde 'Observación del mercado' en MT5.")
            best = suggestions[0]
            if messagebox.askyesno(
                "Símbolo no encontrado",
                f"El símbolo '{symbol}' no existe en este bróker "
                f"(los nombres distinguen mayúsculas).\n\n¿Usar '{best}'?",
                parent=self,
            ) and mt5c.ensure_symbol(best):
                symbol = best
                self.vars["symbol"].set(best)
            else:
                return False, (f"El símbolo '{symbol}' no existe. "
                               f"Sugerencias: {', '.join(suggestions)}")

        spread = mt5c.get_spread(symbol)
        spec = mt5c.get_symbol_spec(symbol)
        lot_txt = f" | lote mín. bróker {spec.volume_min:g}" if spec is not None else ""
        try:
            max_spread = float(self.vars["max_spread"].get())
        except ValueError:
            max_spread = float("nan")
        if spread != spread:  # NaN: sin cotización (mercado cerrado)
            return False, f"{symbol}: sin cotización ahora (¿mercado cerrado?){lot_txt}"
        if not spread <= max_spread:
            return False, (f"{symbol}: spread actual {spread:g} > máximo {max_spread:g}. "
                           f"El bot NO operará: suba 'Spread máximo' (p. ej. {spread * 2:g}) y guarde."
                           f"{lot_txt}")
        return True, f"{symbol}: spread actual {spread:g} <= máximo {max_spread:g}{lot_txt}"

    def on_test_connection(self) -> None:
        """Conecta a MT5, muestra balance/equity y cierra la conexión."""
        self.set_status("Conectando con MetaTrader 5...", COLOR_INFO)
        self.configure(cursor="watch")
        try:
            import mt5_connector as mt5c  # import diferido: la GUI abre aunque falte MT5

            ok = mt5c.connect_mt5_with_retry(
                max_retries=2, wait_seconds=1, require_demo=bool(self.demo_var.get()),
                terminal_path=self.base_config.get("terminal_path") or None,
            )
            if not ok:
                self.set_status(
                    "No se pudo conectar. ¿Está MT5 abierto y con sesión iniciada?", COLOR_ERR
                )
                return
            account = mt5c.mt5.account_info()
            account_txt = (
                f"Conexión OK | login {account.login} ({account.server}) | "
                f"balance {account.balance:.2f} {account.currency} | equity {account.equity:.2f}"
            )
            symbol = self.vars["symbol"].get().strip()
            if not symbol:
                self.set_status(account_txt, COLOR_OK)
                return

            ok_symbol, symbol_txt = self._check_symbol(mt5c, symbol)
            self.set_status(f"{account_txt}\n{symbol_txt}", COLOR_OK if ok_symbol else COLOR_ERR)
        except SystemExit as exc:  # cuenta real con "Solo DEMO" marcado
            self.set_status(str(exc), COLOR_ERR)
        except Exception as exc:  # noqa: BLE001 - mostrar cualquier error en la GUI
            logger.exception("Error probando conexión")
            self.set_status(f"Error: {exc}", COLOR_ERR)
        finally:
            try:
                import mt5_connector as mt5c

                mt5c.safe_shutdown()
            except Exception:  # noqa: BLE001
                pass
            self.configure(cursor="")


def main() -> None:
    """Abre el panel de configuración (bloqueante hasta cerrar la ventana)."""
    setup_logging("config_panel.log")
    app = ConfigPanel()
    app.mainloop()


if __name__ == "__main__":
    main()
