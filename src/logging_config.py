import logging
import sys


def setup_logging(level=logging.INFO, log_file=None):
    """
    Configura o logging do pipeline: handler de console (stdout) e, opcionalmente,
    um arquivo. Idempotente — chamadas repetidas nao duplicam handlers.

    Cada modulo deve obter seu logger com `logging.getLogger(__name__)`; a
    configuracao dos handlers fica centralizada aqui e e acionada uma vez no
    ponto de entrada (main.py).
    """
    root = logging.getLogger()
    root.setLevel(level)

    if getattr(setup_logging, "_configured", False):
        # Permite reajustar o nivel em reexecucoes sem recriar handlers.
        root.setLevel(level)
        return root

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    setup_logging._configured = True
    return root
