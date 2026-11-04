"""
Static capability analysis for ``execute_code`` snippets.

The approval layer uses this module to answer one question before a snippet
runs: *what can this code do?* It is a prompting aid, not an isolation
boundary. Approved code still runs on the host with the user's full
permissions.

Python is analysed with :mod:`ast` (exact and inexpensive). JavaScript,
TypeScript, bash/shell and PowerShell fall back to conservative pattern
matching because this project does not ship parsers for them.

Known blind spots (deliberately documented rather than hidden):

* Dynamic attribute access such as ``getattr(os, "remove")`` is not resolved.
* Runtime module lookup such as ``importlib.import_module("os").remove`` is
  not resolved to a capability.
* Code assembled at runtime (``exec("..." + name)``) is not inspected. The
  ``exec`` call itself is reported as ``eval``, but the constructed code is
  opaque.
* Reflection (``globals()["os"].remove``, ``__builtins__`` lookups, string
  dispatch) is not resolved.
* Variables are not data-flow tracked. ``p = Path("x"); p.write_text(...)``
  is recognised as a write, but its destination is reported as unknown.
* Third-party libraries that write or delete files (pandas ``to_csv``, PIL
  ``save``, database drivers, ...) are not modelled unless they happen to
  match a known module/pattern.
* Non-Python languages use coarse regexes; obfuscation will evade them.

Deletion is tracked separately from writing. ``fs_delete`` is emitted for
known deletion APIs and always requires approval, even inside the workspace:
a write can be reviewed or redone, a delete cannot. Writing new content is
``fs_write``; the approval layer additionally gates a write that targets an
existing file.

Fail-closed rules:

* Unsupported language -> maximally capable.
* Python that does not parse -> maximally capable.
* Any internal analyser exception -> maximally capable.

"Might be capable" is the intended default. The caller decides whether a
capability needs a prompt. This is a prompting control, not a sandbox: it
cannot see every way to reach a filesystem API.
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

CAP_FS_READ = "fs_read"
CAP_FS_WRITE = "fs_write"
CAP_FS_DELETE = "fs_delete"
CAP_PROCESS = "process"
CAP_NETWORK = "network"
CAP_EVAL = "eval"
CAP_ENV = "env"

#: Every capability this module can report. Used when analysis fails closed.
MAX_CAPABILITIES: frozenset = frozenset({
    CAP_FS_READ,
    CAP_FS_WRITE,
    CAP_FS_DELETE,
    CAP_PROCESS,
    CAP_NETWORK,
    CAP_EVAL,
    CAP_ENV,
})

#: Aliases accepted by the execute_code tool, mirrored here so the analyser
#: does not depend on the tool implementation.
_LANGUAGE_ALIASES: Dict[str, str] = {
    "python": "python",
    "python3": "python",
    "py": "python",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "bash": "bash",
    "shell": "bash",
    "sh": "bash",
    "powershell": "powershell",
    "ps1": "powershell",
    "pwsh": "powershell",
}


@dataclass
class AnalysisResult:
    """Capabilities and (for writes/deletes) visible destinations."""

    capabilities: Set[str] = field(default_factory=set)
    write_paths: Set[str] = field(default_factory=set)
    #: Subset of ``write_paths`` that may truncate/overwrite an existing file.
    overwrite_paths: Set[str] = field(default_factory=set)
    unknown_write_path: bool = False
    delete_paths: Set[str] = field(default_factory=set)
    unknown_delete_path: bool = False
    failed: bool = False
    detail: str = ""

    def freeze(self) -> "AnalysisResult":
        self.capabilities = frozenset(self.capabilities)  # type: ignore[assignment]
        self.write_paths = frozenset(self.write_paths)  # type: ignore[assignment]
        self.overwrite_paths = frozenset(self.overwrite_paths)  # type: ignore[assignment]
        self.delete_paths = frozenset(self.delete_paths)  # type: ignore[assignment]
        return self


def _failed(detail: str) -> AnalysisResult:
    return AnalysisResult(
        capabilities=set(MAX_CAPABILITIES),
        write_paths=set(),
        unknown_write_path=True,
        delete_paths=set(),
        unknown_delete_path=True,
        failed=True,
        detail=detail,
    ).freeze()


def normalise_language(language: Optional[str]) -> Optional[str]:
    """Return a canonical language name, or ``None`` if unsupported."""
    if language is None:
        return "python"
    key = str(language).strip().lower()
    if not key:
        return "python"
    return _LANGUAGE_ALIASES.get(key)


def analyse(source: str, language: Optional[str] = "python") -> AnalysisResult:
    """Analyse ``source`` and return its capabilities.

    This function never raises. An unsupported language, a parse failure or
    an internal error produces :data:`MAX_CAPABILITIES` with ``failed=True``.
    """
    try:
        if not isinstance(source, str):
            source = str(source)
        canonical = normalise_language(language)
        if canonical is None:
            return _failed(f"unsupported language {language!r}")
        if canonical == "python":
            return _analyse_python(source)
        return _analyse_patterns(source, canonical)
    except Exception as exc:  # noqa: BLE001 - fail closed, never bypass
        return _failed(f"analyser error: {type(exc).__name__}: {exc}")


# ─── Python analysis ────────────────────────────────────────────────


_EVAL_BUILTINS = {"eval", "exec", "compile", "__import__"}

_OS_DELETE_FUNCS = {"remove", "unlink", "rmdir", "removedirs"}
_OS_WRITE_FUNCS = {
    "rename", "renames", "replace", "mkdir", "makedirs", "mknod", "truncate",
    "open", "chmod", "lchmod", "chown", "lchown", "utime",
}
_OS_READ_FUNCS = {"listdir", "scandir", "walk", "stat", "lstat", "readlink"}
_OS_PROCESS_FUNCS = {
    "system", "popen", "startfile", "fork", "forkpty", "kill", "killpg",
}
_TEMPFILE_WRITE_FUNCS = {
    "mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile",
    "TemporaryDirectory", "SpooledTemporaryFile",
}
_PATH_DELETE_METHODS = {"unlink", "rmdir"}
_PATH_WRITE_METHODS = {
    "write_text", "write_bytes", "mkdir", "touch",
    "rename", "replace", "symlink_to", "hardlink_to", "link_to",
    "chmod", "lchmod",
}
_PATH_READ_METHODS = {"read_text", "read_bytes", "readlink", "iterdir", "glob", "rglob"}
_NETWORK_MODULES = {
    "socket", "urllib", "http", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "telnetlib", "websocket", "websockets",
}
_PROCESS_MODULES = {"subprocess", "pty", "ctypes", "multiprocessing"}


def _module_matches(name: str, modules: Iterable[str]) -> bool:
    return any(name == m or name.startswith(m + ".") for m in modules)


def _same_literal_path(left: str, right: str) -> bool:
    try:
        return (
            os.path.normcase(os.path.abspath(left))
            == os.path.normcase(os.path.abspath(right))
        )
    except (OSError, ValueError):
        return False


class _PythonAnalyser(ast.NodeVisitor):
    def __init__(self) -> None:
        self.aliases: Dict[str, str] = {}
        self.result = AnalysisResult()

    # – alias resolution ──────────────────────────────────────────

    def _collect_aliases(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.aliases[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".")[0]
                        self.aliases[root] = root
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    target = f"{module}.{alias.name}" if module else alias.name
                    self.aliases[local] = target

        # Simple rebound aliases: ``m = os; m.remove(...)``. This is not full
        # data-flow analysis, but catches the obvious case and never resolves
        # values (e.g. paths) that depend on runtime data.
        for _ in range(3):
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                canonical = self._canonical(node.value)
                if not canonical:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and self.aliases.get(target.id) != canonical:
                        self.aliases[target.id] = canonical
                        changed = True
            if not changed:
                break

    def _canonical(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Call):
            return self._canonical(node.func)
        if isinstance(node, ast.Attribute):
            base = self._canonical(node.value)
            if base:
                return f"{base}.{node.attr}"
        return None

    def _canonical_expr(self, node: ast.AST) -> Optional[str]:
        return self._canonical(node)

    # – capability classification ─────────────────────────────────

    def _classify(self, canonical: Optional[str]) -> Optional[str]:
        if not canonical:
            return None
        if canonical.startswith("builtins."):
            canonical = canonical[len("builtins."):]

        if canonical in _EVAL_BUILTINS:
            return CAP_EVAL
        if canonical == "open" or canonical == "io.open":
            return None  # mode-dependent; handled in _handle_call
        if canonical.startswith("importlib."):
            return CAP_EVAL
        if canonical.startswith("getpass."):
            return CAP_ENV

        if canonical.startswith("os."):
            rest = canonical[3:]
            head = rest.split(".")[0]
            if rest == "environ" or rest.startswith("environ.") or rest in {"getenv", "getenvb"}:
                return CAP_ENV
            if rest in _OS_DELETE_FUNCS or head in _OS_DELETE_FUNCS:
                return CAP_FS_DELETE
            if rest in _OS_WRITE_FUNCS or head in _OS_WRITE_FUNCS:
                return CAP_FS_WRITE
            if rest in _OS_READ_FUNCS:
                return CAP_FS_READ
            if (head in _OS_PROCESS_FUNCS or head.startswith("exec")
                    or head.startswith("spawn")):
                return CAP_PROCESS
            return None

        if canonical.startswith("glob."):
            return CAP_FS_READ
        if canonical == "shutil.rmtree":
            return CAP_FS_DELETE
        if canonical.startswith("shutil."):
            return CAP_FS_WRITE
        if canonical.startswith("tempfile.") and canonical.split(".", 1)[1] in _TEMPFILE_WRITE_FUNCS:
            return CAP_FS_WRITE

        if canonical.startswith("pathlib."):
            tail = canonical.rsplit(".", 1)[-1]
            if tail in _PATH_DELETE_METHODS:
                return CAP_FS_DELETE
            if tail in _PATH_WRITE_METHODS:
                return CAP_FS_WRITE
            if tail in _PATH_READ_METHODS:
                return CAP_FS_READ
            return None

        if _module_matches(canonical, _PROCESS_MODULES):
            return CAP_PROCESS
        if canonical == "asyncio.create_subprocess_exec" or canonical == "asyncio.create_subprocess_shell":
            return CAP_PROCESS
        if _module_matches(canonical, _NETWORK_MODULES):
            return CAP_NETWORK
        return None

    def _mark(self, capability: Optional[str]) -> None:
        if capability:
            self.result.capabilities.add(capability)

    def _record_write(self, path: Optional[str]) -> None:
        if path is None:
            self.result.unknown_write_path = True
        else:
            self.result.write_paths.add(path)

    def _record_overwrite(self, path: Optional[str]) -> None:
        """Record a write that may replace or truncate existing content."""
        self._record_write(path)
        if path is not None:
            self.result.overwrite_paths.add(path)

    def _record_delete(self, path: Optional[str]) -> None:
        if path is None:
            self.result.unknown_delete_path = True
        else:
            self.result.delete_paths.add(path)

    # – literal path extraction ───────────────────────────────────

    def _literal_str(self, node: Optional[ast.AST]) -> Optional[str]:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr) and all(
            isinstance(v, ast.Constant) and isinstance(v.value, str)
            for v in node.values
        ):
            return "".join(v.value for v in node.values)  # type: ignore[union-attr]
        return None

    def _literal_path(self, node: Optional[ast.AST]) -> Optional[str]:
        """Return a literal path string, or ``None`` if it is not static.

        Only strings, f-strings without placeholders, ``os.path.join`` of
        literals and ``Path``/``/`` composition of literals are understood.
        """
        direct = self._literal_str(node)
        if direct is not None:
            return direct
        if isinstance(node, ast.Call):
            canonical = self._canonical(node.func)
            if canonical in {"os.path.join", "posixpath.join", "ntpath.join"}:
                values = [self._literal_path(arg) for arg in node.args]
                if values and all(v is not None for v in values):
                    return os.path.join(*values)  # type: ignore[arg-type]
            if canonical in {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath"}:
                if node.args:
                    return self._literal_path(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self._literal_path(node.left)
            right = self._literal_str(node.right)
            if left is not None and right is not None:
                return os.path.join(left, right)
        return None

    @staticmethod
    def _arg(call: ast.Call, index: int, keyword: Optional[str] = None) -> Optional[ast.AST]:
        if len(call.args) > index:
            return call.args[index]
        if keyword:
            for item in call.keywords:
                if item.arg == keyword:
                    return item.value
        return None

    @staticmethod
    def _mode_is_write(mode_node: Optional[ast.AST]) -> bool:
        if mode_node is None:
            return False
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            return any(ch in mode_node.value for ch in "wax+")
        # Non-literal mode: assume it may write.
        return True

    # – visitors ──────────────────────────────────────────────────

    def visit_Module(self, node: ast.Module) -> None:
        self._collect_aliases(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        canonical = self.aliases.get(node.id)
        if canonical and (
            canonical.startswith("os.environ")
            or canonical in {"os.getenv", "os.getenvb"}
        ):
            self._mark(CAP_ENV)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        canonical = self._canonical(node)
        if canonical:
            # Mark references too, so ``x = os.environ`` is not silent.
            self._mark(self._classify(canonical))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        canonical = self._canonical(node.func)
        if canonical is None and isinstance(node.func, ast.Name):
            # Builtins are not imported, so they have no alias entry.
            if node.func.id in {"open", "eval", "exec", "compile", "__import__"}:
                canonical = node.func.id
        attr_name: Optional[str] = None
        if isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if canonical is None:
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id not in self.aliases:
                    # A plain local variable (e.g. ``p.write_text``): the
                    # receiver is not a known module, so leave the call
                    # unresolved and record an unknown destination below.
                    base = None
                else:
                    base = self._canonical_expr(receiver)
                if base:
                    canonical = f"{base}.{attr_name}"

        if canonical:
            self._mark(self._classify(canonical))

        # open() and Path.open() need their mode inspected.
        if canonical in {"open", "io.open", "builtins.open", "pathlib.Path.open"}:
            self._handle_open(node, canonical)
        elif canonical and canonical.startswith("pathlib.") and attr_name in _PATH_DELETE_METHODS:
            self._handle_path_delete(node)
        elif canonical and canonical.startswith("pathlib.") and attr_name in _PATH_WRITE_METHODS:
            self._handle_path_write(node, attr_name)
        elif canonical and canonical.startswith("os."):
            self._handle_os_call(node, canonical)
        elif canonical and canonical.startswith("shutil."):
            self._handle_shutil_call(node, canonical)
        elif canonical and canonical.startswith("tempfile."):
            self._handle_tempfile_call(node, canonical)
        elif canonical is None and attr_name in _PATH_DELETE_METHODS:
            # Receiver is a variable we cannot resolve. Still mark the delete,
            # but the destination is unknown.
            self._mark(CAP_FS_DELETE)
            self._record_delete(self._literal_path(node.func.value))  # type: ignore[union-attr]
        elif canonical is None and attr_name in _PATH_WRITE_METHODS:
            # Receiver is a variable we cannot resolve. Still mark the write,
            # but the destination is unknown.
            self._mark(CAP_FS_WRITE)
            receiver_path = self._literal_path(node.func.value)  # type: ignore[union-attr]
            if attr_name in {"write_text", "write_bytes", "rename", "replace"}:
                self._record_overwrite(receiver_path)
            else:
                self._record_write(receiver_path)
            if attr_name in {"rename", "replace"}:
                self._record_overwrite(self._literal_path(self._arg(node, 0)))
            elif attr_name in {"symlink_to", "hardlink_to", "link_to"}:
                self._record_write(self._literal_path(self._arg(node, 0)))
        elif canonical is None and attr_name in _PATH_READ_METHODS:
            self._mark(CAP_FS_READ)

        self.generic_visit(node)

    # – call handlers ─────────────────────────────────────────────

    def _handle_open(self, node: ast.Call, canonical: str) -> None:
        if canonical == "pathlib.Path.open":
            path = self._literal_path(node.func.value)  # type: ignore[attr-defined]
            mode_node = self._arg(node, 0, "mode")
        else:
            path = self._literal_path(self._arg(node, 0))
            mode_node = self._arg(node, 1, "mode")
        if self._mode_is_write(mode_node):
            self._mark(CAP_FS_WRITE)
            self._record_overwrite(path)
        else:
            self._mark(CAP_FS_READ)

    def _handle_path_delete(self, node: ast.Call) -> None:
        self._mark(CAP_FS_DELETE)
        receiver = node.func.value  # type: ignore[attr-defined]
        self._record_delete(self._literal_path(receiver))

    def _handle_path_write(self, node: ast.Call, attr_name: str) -> None:
        self._mark(CAP_FS_WRITE)
        receiver = node.func.value  # type: ignore[attr-defined]
        if attr_name in {"write_text", "write_bytes"}:
            self._record_overwrite(self._literal_path(receiver))
        else:
            self._record_write(self._literal_path(receiver))
        if attr_name in {"rename", "replace"}:
            self._record_overwrite(self._literal_path(receiver))
            self._record_overwrite(self._literal_path(self._arg(node, 0)))
        elif attr_name in {"symlink_to", "hardlink_to", "link_to"}:
            self._record_write(self._literal_path(self._arg(node, 0)))

    def _handle_os_call(self, node: ast.Call, canonical: str) -> None:
        rest = canonical[3:]
        if rest in _OS_DELETE_FUNCS:
            self._mark(CAP_FS_DELETE)
            self._record_delete(self._literal_path(self._arg(node, 0)))
        elif rest in {"rename", "renames", "replace"}:
            self._record_overwrite(self._literal_path(self._arg(node, 0)))
            self._record_overwrite(self._literal_path(self._arg(node, 1)))
        elif rest in {"truncate", "open"}:
            # os.open can truncate (O_TRUNC) or create/overwrite (O_CREAT).
            self._record_overwrite(self._literal_path(self._arg(node, 0)))
        elif rest in _OS_WRITE_FUNCS:
            self._record_write(self._literal_path(self._arg(node, 0)))

    def _handle_shutil_call(self, node: ast.Call, canonical: str) -> None:
        name = canonical.rsplit(".", 1)[-1]
        if name == "rmtree":
            self._mark(CAP_FS_DELETE)
            self._record_delete(self._literal_path(self._arg(node, 0)))
        elif name == "move":
            self._mark(CAP_FS_WRITE)
            source = self._literal_path(self._arg(node, 0))
            destination = self._literal_path(self._arg(node, 1))
            self._record_overwrite(destination)
            # A move removes the source. Only skip the delete capability when
            # both literal paths are provably the same; an unknown destination
            # is treated as different (fail closed).
            if source is None or destination is None or not _same_literal_path(source, destination):
                self._mark(CAP_FS_DELETE)
                self._record_delete(source)
        elif name in {"copy", "copyfile", "copy2", "copytree"}:
            self._record_write(self._literal_path(self._arg(node, 0)))
            self._record_overwrite(self._literal_path(self._arg(node, 1)))
        else:
            self._record_write(self._literal_path(self._arg(node, 0)))

    def _handle_tempfile_call(self, node: ast.Call, canonical: str) -> None:
        name = canonical.rsplit(".", 1)[-1]
        if name not in _TEMPFILE_WRITE_FUNCS:
            return
        if name in {"mkstemp", "mkdtemp"}:
            dir_node = self._arg(node, 2, "dir")
        else:
            dir_node = None
            for item in node.keywords:
                if item.arg == "dir":
                    dir_node = item.value
                    break
        self._record_write(self._literal_path(dir_node))


def _analyse_python(source: str) -> AnalysisResult:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return _failed(f"could not parse Python: {exc}")
    analyser = _PythonAnalyser()
    analyser.visit(tree)
    result = analyser.result
    if not result.write_paths and CAP_FS_WRITE not in result.capabilities:
        result.unknown_write_path = False
    if not result.delete_paths and CAP_FS_DELETE not in result.capabilities:
        result.unknown_delete_path = False
    return result.freeze()


# ─── Other languages (conservative patterns) ────────────────────────


_PATTERNS: Dict[str, List[Tuple["re.Pattern[str]", str]]] = {
    "javascript": [
        (re.compile(r"\bfs(?:\.promises)?\.(?:writeFile|appendFile|truncate|ftruncate|"
                    r"unlink|rm|rmdir|mkdir|mkdtemp|createWriteStream|"
                    r"writeFileSync|appendFileSync|unlinkSync|rmSync|rmdirSync|"
                    r"mkdirSync)\b"),
         CAP_FS_WRITE),
        (re.compile(r"\b(?:Deno\.(?:writeTextFile|writeFile|remove|mkdir|create)|"
                    r"Bun\.(?:write|file))\b"), CAP_FS_WRITE),
        (re.compile(r"\bfs(?:\.promises)?\.(?:readFile|readdir|createReadStream|"
                    r"readFileSync|readdirSync|stat|statSync)\b"), CAP_FS_READ),
        (re.compile(r"\b(?:child_process|execSync|execFileSync|spawnSync|"
                    r"Deno\.run|Bun\.spawn)\b"), CAP_PROCESS),
        (re.compile(r"\b(?:exec|execFile|spawn|fork)\s*\("), CAP_PROCESS),
        (re.compile(r"\b(?:https?|net|tls|dgram)\.|"
                    r"\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|"
                    r"\baxios\b|\bnode-fetch\b"), CAP_NETWORK),
        (re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(|\bvm\.runIn"), CAP_EVAL),
        (re.compile(r"\bprocess\.env\b|\bDeno\.env\b|\bBun\.env\b"), CAP_ENV),
    ],
    "typescript": [
        (re.compile(r"\bfs(?:\.promises)?\.(?:writeFile|appendFile|truncate|ftruncate|"
                    r"unlink|rm|rmdir|mkdir|mkdtemp|createWriteStream|"
                    r"writeFileSync|appendFileSync|unlinkSync|rmSync|rmdirSync|"
                    r"mkdirSync)\b"),
         CAP_FS_WRITE),
        (re.compile(r"\b(?:Deno\.(?:writeTextFile|writeFile|remove|mkdir|create)|"
                    r"Bun\.(?:write|file))\b"), CAP_FS_WRITE),
        (re.compile(r"\bfs(?:\.promises)?\.(?:readFile|readdir|createReadStream|"
                    r"readFileSync|readdirSync|stat|statSync)\b"), CAP_FS_READ),
        (re.compile(r"\b(?:child_process|execSync|execFileSync|spawnSync|"
                    r"Deno\.run|Bun\.spawn)\b"), CAP_PROCESS),
        (re.compile(r"\b(?:exec|execFile|spawn|fork)\s*\("), CAP_PROCESS),
        (re.compile(r"\b(?:https?|net|tls|dgram)\.|"
                    r"\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|"
                    r"\baxios\b|\bnode-fetch\b"), CAP_NETWORK),
        (re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(|\bvm\.runIn"), CAP_EVAL),
        (re.compile(r"\bprocess\.env\b|\bDeno\.env\b|\bBun\.env\b"), CAP_ENV),
    ],
    "bash": [
        (re.compile(r"(?:^|[^<])(?:>>?|&>)\s*\S|\btee\b|\btouch\b|\bmkdir\b|"
                    r"\brmdir\b|\brm\b|\bmv\b|\bcp\b|\bdd\b|\btruncate\b|"
                    r"\bsed\s+-i|\bchmod\b|\bchown\b|\binstall\b"),
         CAP_FS_WRITE),
        (re.compile(r"\b(?:cat|less|more|head|tail|grep|rg|ls|find|stat|"
                    r"readlink)\b|<\s*[^<]"), CAP_FS_READ),
        (re.compile(r"\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bssh\b|\bscp\b|"
                    r"\bsftp\b|\bftp\b|/dev/tcp/"), CAP_NETWORK),
        (re.compile(r"\beval\b|\bsource\b|\.\s+\S"), CAP_EVAL),
        (re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\benv\b|\bprintenv\b"),
         CAP_ENV),
    ],
    "powershell": [
        (re.compile(r"\b(?:Remove-Item|Set-Content|Add-Content|Out-File|New-Item|"
                    r"Copy-Item|Move-Item|Rename-Item|Clear-Content|"
                    r"Set-ItemProperty|Remove-ItemProperty)\b|\bdel\b|\brm\b",
                    re.IGNORECASE),
         CAP_FS_WRITE),
        (re.compile(r"\b(?:Get-Content|Get-ChildItem|Select-String)\b", re.IGNORECASE),
         CAP_FS_READ),
        (re.compile(r"\b(?:Invoke-WebRequest|Invoke-RestMethod|Start-BitsTransfer|"
                    r"System\.Net\.|New-Object\s+Net\.)\b", re.IGNORECASE),
         CAP_NETWORK),
        (re.compile(r"\b(?:Invoke-Expression|iex)\b", re.IGNORECASE), CAP_EVAL),
        (re.compile(r"\$env:|\bGet-ChildItem\s+Env:", re.IGNORECASE), CAP_ENV),
    ],
}

#: Shell languages always run through a command interpreter, so they always
#: have process capability regardless of their contents.
_ALWAYS_PROCESS = {"bash", "powershell"}


def _analyse_patterns(source: str, language: str) -> AnalysisResult:
    result = AnalysisResult()
    if language in _ALWAYS_PROCESS and source.strip():
        result.capabilities.add(CAP_PROCESS)
    for pattern, capability in _PATTERNS.get(language, []):
        if pattern.search(source):
            result.capabilities.add(capability)
    if CAP_FS_WRITE in result.capabilities:
        # Non-Python path extraction is not attempted: we cannot reliably tell
        # whether the destination is inside the workspace, so treat it as
        # unknown (which prompts).
        result.unknown_write_path = True
    return result.freeze()
