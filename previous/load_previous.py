"""Disponibilize previous/ sem exigir mudanças nos imports das etapas antigas.

Os módulos continuam sendo importados por seus nomes originais, como ast_nodes
e symbols. Assim, todas as passagens compartilham as mesmas classes e símbolos.
Os módulos da raiz têm prioridade, incluindo os diagnósticos atualizados.
"""

from pathlib import Path
import sys


PROJECT_DIR = str(Path(__file__).resolve().parent)
PREVIOUS_DIR = str(Path(PROJECT_DIR) / "previous")
sys.path[:] = [PROJECT_DIR, PREVIOUS_DIR] + [
    directory for directory in sys.path
    if directory not in {PROJECT_DIR, PREVIOUS_DIR}
]
