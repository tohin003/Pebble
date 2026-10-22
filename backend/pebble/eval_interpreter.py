"""Restricted-Python grammar gate and host-owned interpreter for graded code.

Oracle-free by construction: this module imports only the standard library,
holds no corpus, oracle or expected value, and never imports pebble.evaluation.
The grading child imports this module alone, so its memory holds nothing to leak.

Candidate code never runs as native Python. The gate admits an exact grammar,
names, module members and value methods; the interpreter re-checks every rule
at run time under operation, logical-size, integer, call-depth and wall-time
budgets. Resource stops (`_Halt`) cannot be caught, and no candidate except,
else or finally code runs after one.

This is language isolation only. Process isolation (timeout, rlimits and the
macOS no-network/no-write profile) is applied by the caller; reads are not
confined there, and no other-platform sandbox is assumed.
"""

import ast
import functools as _functools
import math as _math
import operator as _operator
import re
import statistics as _statistics
import time

SIZE_LIMIT, BIT_LIMIT, OP_LIMIT, CALL_DEPTH, TIME_LIMIT = 100_000, 4096, 100_000, 24, 2.0


class _Halt(BaseException):
    """Interpreter resource/construct stop; candidate except clauses cannot catch it."""


class _Return(BaseException):
    def __init__(self, value):
        super().__init__()
        self.value = value


class _Break(BaseException):
    pass


class _Continue(BaseException):
    pass


class _ModuleRef:
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name


class _Function:
    __slots__ = ('interpreter', 'node', 'scopes', 'defaults')

    def __init__(self, interpreter, node, scopes, defaults):
        self.interpreter, self.node, self.scopes, self.defaults = interpreter, node, scopes, defaults

    def __call__(self, *args, **kwargs):
        return self.interpreter.invoke(self, list(args), kwargs)


class _Method:
    """An admitted value method. However it is reached (called directly, stored,
    passed or defaulted), every call re-checks its owner and arguments before
    and after the real method runs."""
    __slots__ = ('interpreter', 'owner', 'bound')

    def __init__(self, interpreter, owner, bound):
        self.interpreter, self.owner, self.bound = interpreter, owner, bound

    def __call__(self, *args, **kwargs):
        interpreter = self.interpreter
        interpreter.tick()
        interpreter.measure(self.owner)
        for value in (*args, *kwargs.values()):
            interpreter.measure(value)
        result = self.bound(*args, **kwargs)
        interpreter.measure(self.owner)
        return interpreter.check(result)


_NESTED = frozenset({list, tuple, str})


def deep_size(value, limit=SIZE_LIMIT):
    """Logical size, stopping just past `limit`.

    Each list/tuple occurrence counts one plus its slots, each string its
    characters and each range its length. Shared references count at every
    occurrence, so repetition and cycles are measured as the work they cause.
    Iterative: cycles end when the count passes the limit, without recursion.
    """
    kind = type(value)
    if kind is str or kind is range:
        return len(value)
    if kind is not list and kind is not tuple:
        return 0
    total, stack = 0, [value]
    while stack:
        item = stack.pop()
        total += 1 + len(item)
        if total > limit:
            return total
        if any(map(_NESTED.__contains__, map(type, item))):
            for child in item:
                kind = type(child)
                if kind is str:
                    total += len(child)
                    if total > limit:
                        return total
                elif kind is list or kind is tuple:
                    stack.append(child)
    return total


def _safe_range(*args):
    if not 1 <= len(args) <= 3 or any(type(a) is not int for a in args):
        raise TypeError('range() takes one to three integers here')
    value = range(*args)
    try:
        size = len(value)
    except OverflowError:
        raise _Halt('value_budget') from None
    if size > SIZE_LIMIT:
        raise _Halt('value_budget')
    return value


def _safe_sum(values, start=0):
    items = list(values)
    if len(items) > SIZE_LIMIT or type(start) not in (int, float) or any(type(v) not in (int, float, bool) for v in items):
        raise TypeError('sum() accepts numbers only here')
    return sum(items, start)


def _safe_round(number, ndigits=None):
    if type(number) not in (int, float, bool) or ndigits is not None and (type(ndigits) is not int or abs(ndigits) > 32):
        raise TypeError('round() accepts a number and a small ndigits here')
    return round(number) if ndigits is None else round(number, ndigits)


MODULE_MEMBERS = {
    'math': {name: getattr(_math, name) for name in ('floor', 'ceil', 'fsum', 'isfinite', 'isnan', 'isclose',
                                                     'inf', 'nan', 'trunc', 'sqrt', 'fabs')},
    'statistics': {name: getattr(_statistics, name) for name in ('median', 'mean', 'fmean', 'median_low',
                                                                 'median_high', 'StatisticsError')},
    'functools': {'reduce': _functools.reduce, 'cmp_to_key': _functools.cmp_to_key},
}
VALUE_METHODS = {list: frozenset({'sort', 'copy', 'append', 'extend', 'index', 'count', 'pop', 'insert', 'reverse'}),
                 tuple: frozenset({'index', 'count'}), float: frozenset({'is_integer'})}
ALL_METHODS = frozenset().union(*VALUE_METHODS.values())
SAFE_BUILTINS = {
    'len': len, 'sorted': sorted, 'sum': _safe_sum, 'min': min, 'max': max, 'abs': abs, 'float': float, 'int': int,
    'str': str, 'bool': bool, 'list': list, 'tuple': tuple, 'range': _safe_range, 'enumerate': enumerate,
    'zip': zip, 'reversed': reversed, 'isinstance': isinstance, 'round': _safe_round, 'divmod': divmod,
    'all': all, 'any': any, 'ValueError': ValueError, 'TypeError': TypeError, 'IndexError': IndexError,
    'ZeroDivisionError': ZeroDivisionError, 'ArithmeticError': ArithmeticError, 'Exception': Exception,
}
# Constant-cost builtins whose arguments need no logical-size pre-check.
CHEAP_CALLS = frozenset(map(id, (len, isinstance, bool)))
BINARY = {ast.Add: _operator.add, ast.Sub: _operator.sub, ast.Mult: _operator.mul, ast.Div: _operator.truediv,
          ast.FloorDiv: _operator.floordiv, ast.Mod: _operator.mod, ast.Pow: _operator.pow}
COMPARE = {ast.Eq: _operator.eq, ast.NotEq: _operator.ne, ast.Lt: _operator.lt, ast.LtE: _operator.le,
           ast.Gt: _operator.gt, ast.GtE: _operator.ge, ast.In: lambda a, b: a in b,
           ast.NotIn: lambda a, b: a not in b, ast.Is: _operator.is_, ast.IsNot: _operator.is_not}
GRAMMAR = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign,
           ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass, ast.Raise, ast.Try, ast.ExceptHandler,
           ast.Expr, ast.Assert, ast.Import, ast.ImportFrom, ast.alias, ast.Constant, ast.Name, ast.Load, ast.Store,
           ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.keyword, ast.IfExp, ast.Attribute,
           ast.Subscript, ast.Slice, ast.List, ast.Tuple, ast.ListComp, ast.GeneratorExp, ast.comprehension,
           ast.Lambda, ast.JoinedStr, ast.FormattedValue, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
           *BINARY, *COMPARE)
HALT_REASONS = frozenset({'operation_budget', 'value_budget', 'call_depth', 'time_budget', 'construct',
                          'function_missing', 'static_gate'})


def _grammar_nodes(tree):
    """Every node except annotations, which the interpreter never evaluates."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for name, value in ast.iter_fields(node):
            if name in {'annotation', 'returns'}:
                continue
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, ast.AST):
                    stack.append(item)


def _dunder(name):
    return isinstance(name, str) and name.startswith('__')


def static_gate(source, function, parameters=1):
    """Exact grammar/name/member gate; returns a reason code or None."""
    if source is None:
        return 'module_missing'
    if len(source) > 20_000:
        return 'module_too_large'
    try:
        tree = ast.parse(source.decode('utf-8'))
    except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        return 'module_unparsable'
    nodes = list(_grammar_nodes(tree))
    aliases, bound, attribute_bases = {}, set(), set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in MODULE_MEMBERS:
                    return 'import_not_allowed'
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            members = {'annotations': None} if node.module == '__future__' else MODULE_MEMBERS.get(node.module)
            if node.level or members is None or any(alias.name not in members for alias in node.names):
                return 'import_not_allowed'
            bound.update(alias.asname or alias.name for alias in node.names)
    if set(aliases) & bound:
        return 'name_not_allowed'
    for node in nodes:
        if not isinstance(node, GRAMMAR):
            return 'construct_not_allowed'
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not any(node is top for top in tree.body):
            return 'import_not_allowed'
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            spec = node.args
            if spec.vararg or spec.kwarg or spec.kwonlyargs or spec.kw_defaults:
                return 'construct_not_allowed'
            if isinstance(node, ast.FunctionDef):
                if node.decorator_list or getattr(node, 'type_params', None):
                    return 'construct_not_allowed'
                bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.Constant) and type(node.value) not in (int, float, str, bool, type(None)):
            return 'construct_not_allowed'
        elif isinstance(node, ast.keyword) and node.arg is None:
            return 'construct_not_allowed'
        elif isinstance(node, ast.comprehension) and node.is_async:
            return 'construct_not_allowed'
        elif isinstance(node, ast.FormattedValue) and node.conversion not in (-1, 97, 114, 115):
            return 'construct_not_allowed'
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.ctx, ast.Load):
                return 'attribute_not_allowed'
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                if node.attr not in MODULE_MEMBERS[aliases[node.value.id]]:
                    return 'attribute_not_allowed'
                attribute_bases.add(id(node.value))
            elif node.attr not in ALL_METHODS:
                return 'attribute_not_allowed'
    if any(_dunder(name) or name in aliases for name in bound):
        return 'name_not_allowed'
    for node in nodes:
        if isinstance(node, ast.Name):
            if _dunder(node.id):
                return 'name_not_allowed'
            if node.id in aliases:
                if not isinstance(node.ctx, ast.Load) or id(node) not in attribute_bases:
                    return 'module_object_escape'
            elif node.id not in bound and node.id not in SAFE_BUILTINS:
                return 'name_not_allowed'
    targets = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
    if len(targets) != 1:
        return 'function_missing'
    if len(targets[0].args.posonlyargs) + len(targets[0].args.args) != parameters:
        return 'signature_changed'
    return None


class _Interpreter:
    """Evaluates only the gate grammar over explicit name/member/method tables.

    No import system, attribute traversal, I/O or exit path exists here. Every
    statement, expression, loop item and method call consumes the operation
    budget; work outside ticks (real builtins, comparisons, formatting) is
    preceded by a logical-size check of its inputs.
    """

    def __init__(self, tree):
        self.ops, self.depth, self.handling, self.globals = 0, 0, [], {}
        self.deadline = time.monotonic() + TIME_LIMIT
        self.run(tree.body, [self.globals])

    def tick(self):
        self.ops += 1
        if self.ops > OP_LIMIT:
            raise _Halt('operation_budget')
        if self.ops % 256 == 0 and time.monotonic() > self.deadline:
            raise _Halt('time_budget')

    @staticmethod
    def measure(value):
        if type(value) is int and value.bit_length() > BIT_LIMIT:
            raise _Halt('value_budget')
        size = deep_size(value)
        if size > SIZE_LIMIT:
            raise _Halt('value_budget')
        return size

    def check(self, value):
        self.measure(value)
        return value

    def run(self, body, scopes):
        for node in body:
            self.statement(node, scopes)

    def statement(self, node, scopes):
        self.tick()
        kind = type(node)
        if kind is ast.Expr:
            self.expr(node.value, scopes)
        elif kind is ast.Assign:
            value = self.expr(node.value, scopes)
            for target in node.targets:
                self.assign(target, value, scopes)
        elif kind is ast.AnnAssign:
            if node.value is not None:
                self.assign(node.target, self.expr(node.value, scopes), scopes)
        elif kind is ast.AugAssign:
            current = self.expr(node.target, scopes)
            self.assign(node.target, self.binary(node.op, current, self.expr(node.value, scopes)), scopes)
        elif kind is ast.FunctionDef:
            scopes[-1][node.name] = _Function(self, node, scopes, [self.expr(d, scopes) for d in node.args.defaults])
        elif kind is ast.Return:
            raise _Return(None if node.value is None else self.expr(node.value, scopes))
        elif kind is ast.If:
            self.run(node.body if self.expr(node.test, scopes) else node.orelse, scopes)
        elif kind in (ast.For, ast.While):
            self.loop(node, scopes)
        elif kind is ast.Break:
            raise _Break()
        elif kind is ast.Continue:
            raise _Continue()
        elif kind is ast.Raise:
            self.throw(node, scopes)
        elif kind is ast.Try:
            self.attempt(node, scopes)
        elif kind is ast.Assert:
            if not self.expr(node.test, scopes):
                raise AssertionError('assertion failed')
        elif kind is ast.Import:
            for alias in node.names:
                if alias.name not in MODULE_MEMBERS:
                    raise _Halt('construct')
                scopes[-1][alias.asname or alias.name] = _ModuleRef(alias.name)
        elif kind is ast.ImportFrom:
            if node.module != '__future__':
                members = MODULE_MEMBERS.get(node.module)
                for alias in node.names:
                    if members is None or alias.name not in members:
                        raise _Halt('construct')
                    scopes[-1][alias.asname or alias.name] = members[alias.name]
        elif kind is not ast.Pass:
            raise _Halt('construct')

    def expr(self, node, scopes):
        self.tick()
        kind = type(node)
        if kind is ast.Constant:
            if type(node.value) not in (int, float, str, bool, type(None)):
                raise _Halt('construct')
            return node.value
        if kind is ast.Name:
            return self.lookup(node.id, scopes)
        if kind is ast.BinOp:
            return self.binary(node.op, self.expr(node.left, scopes), self.expr(node.right, scopes))
        if kind is ast.UnaryOp:
            value = self.expr(node.operand, scopes)
            if type(node.op) is ast.Not:
                return not value
            if type(value) not in (int, float, bool):
                raise TypeError('bad operand type for unary operator')
            return -value if type(node.op) is ast.USub else +value
        if kind is ast.BoolOp:
            for item in node.values:
                value = self.expr(item, scopes)
                if bool(value) is (type(node.op) is ast.Or):
                    return value
            return value
        if kind is ast.Compare:
            left = self.expr(node.left, scopes)
            for op, comparator in zip(node.ops, node.comparators):
                right = self.expr(comparator, scopes)
                self.measure(left)
                self.measure(right)
                if not COMPARE[type(op)](left, right):
                    return False
                left = right
            return True
        if kind is ast.IfExp:
            return self.expr(node.body if self.expr(node.test, scopes) else node.orelse, scopes)
        if kind is ast.Call:
            return self.call(node, scopes)
        if kind is ast.Attribute:
            return self.member(self.expr(node.value, scopes), node.attr)
        if kind is ast.Subscript:
            container = self.expr(node.value, scopes)
            if type(container) not in (list, tuple, str, range):
                raise TypeError('object is not subscriptable here')
            return container[self.key(node.slice, scopes)]
        if kind in (ast.List, ast.Tuple):
            items = [self.expr(item, scopes) for item in node.elts]
            return self.check(items if kind is ast.List else tuple(items))
        if kind in (ast.ListComp, ast.GeneratorExp):
            out, size = [], [1]
            self.comprehend(node, 0, scopes + [{}], out, size)
            return out
        if kind is ast.Lambda:
            return _Function(self, node, scopes, [self.expr(d, scopes) for d in node.args.defaults])
        if kind is ast.JoinedStr:
            return self.check(''.join(item.value if type(item) is ast.Constant else self.formatted(item, scopes)
                                      for item in node.values))
        raise _Halt('construct')

    def lookup(self, name, scopes):
        for scope in reversed(scopes):
            if name in scope:
                return scope[name]
        if name in SAFE_BUILTINS:
            return SAFE_BUILTINS[name]
        raise NameError(name)

    def member(self, owner, name):
        if type(owner) is _ModuleRef:
            members = MODULE_MEMBERS.get(owner.name, {})
            if name not in members:
                raise AttributeError(name)
            return members[name]
        if name not in VALUE_METHODS.get(type(owner), ()):
            raise AttributeError(name)
        return _Method(self, owner, getattr(owner, name))

    def key(self, node, scopes):
        if type(node) is ast.Slice:
            return slice(*(None if part is None else self.expr(part, scopes)
                           for part in (node.lower, node.upper, node.step)))
        return self.expr(node, scopes)

    def formatted(self, node, scopes):
        value = self.expr(node.value, scopes)
        spec = '' if node.format_spec is None else self.expr(node.format_spec, scopes)
        if not isinstance(spec, str) or re.search(r'\d{4,}', spec):
            raise _Halt('value_budget')
        self.measure(value)
        if node.conversion == 115:
            value = str(value)
        elif node.conversion == 114:
            value = repr(value)
        elif node.conversion == 97:
            value = ascii(value)
        return self.check(format(value, spec))

    def comprehend(self, node, index, scopes, out, size):
        if index == len(node.generators):
            value = self.expr(node.elt, scopes)
            size[0] += 1 + self.measure(value)
            if size[0] > SIZE_LIMIT:
                raise _Halt('value_budget')
            out.append(value)
            return
        generator = node.generators[index]
        for item in self.iterate(self.expr(generator.iter, scopes)):
            self.assign(generator.target, item, scopes)
            if all(self.expr(condition, scopes) for condition in generator.ifs):
                self.comprehend(node, index + 1, scopes, out, size)

    def iterate(self, value):
        if type(value) in (_Function, _ModuleRef, _Method):
            raise TypeError('object is not iterable')
        for item in value:
            self.tick()
            yield item

    def call(self, node, scopes):
        if type(node.func) is ast.Attribute:
            func = self.member(self.expr(node.func.value, scopes), node.func.attr)
        else:
            func = self.expr(node.func, scopes)
        args = [self.expr(item, scopes) for item in node.args]
        if any(item.arg is None for item in node.keywords):
            raise _Halt('construct')
        kwargs = {item.arg: self.expr(item.value, scopes) for item in node.keywords}
        if type(func) is _Function:
            return self.check(self.invoke(func, args, kwargs))
        if type(func) is _ModuleRef or not callable(func):
            raise TypeError('object is not callable')
        if id(func) not in CHEAP_CALLS:
            for value in (*args, *kwargs.values()):
                self.measure(value)
        return self.check(func(*args, **kwargs))

    def invoke(self, function, args, kwargs):
        self.depth += 1
        try:
            if self.depth > CALL_DEPTH:
                raise _Halt('call_depth')
            spec = function.node.args
            names = [item.arg for item in spec.posonlyargs + spec.args]
            if len(args) > len(names):
                raise TypeError('too many positional arguments')
            local = dict(zip(names, args))
            positional = {item.arg for item in spec.posonlyargs}
            for name, value in kwargs.items():
                if name not in names or name in local or name in positional:
                    raise TypeError('unexpected keyword argument')
                local[name] = value
            defaults = dict(zip(names[len(names) - len(function.defaults):], function.defaults))
            for name in names:
                if name not in local:
                    if name not in defaults:
                        raise TypeError('missing required argument')
                    local[name] = defaults[name]
            scopes = function.scopes + [local]
            if type(function.node) is ast.Lambda:
                return self.expr(function.node.body, scopes)
            try:
                self.run(function.node.body, scopes)
            except _Return as signal:
                return signal.value
            return None
        finally:
            self.depth -= 1

    def assign(self, target, value, scopes):
        kind = type(target)
        if kind is ast.Name:
            scopes[-1][target.id] = value
        elif kind in (ast.Tuple, ast.List):
            items = list(self.iterate(value))
            if len(items) != len(target.elts):
                raise ValueError('unpacking length mismatch')
            for element, item in zip(target.elts, items):
                self.assign(element, item, scopes)
        elif kind is ast.Subscript:
            container = self.expr(target.value, scopes)
            if type(container) is not list:
                raise TypeError('only list items can be assigned here')
            self.measure(value)
            container[self.key(target.slice, scopes)] = value
            self.measure(container)
        else:
            raise _Halt('construct')

    def binary(self, op, left, right):
        kind = type(op)
        numbers = (int, float, bool)
        if kind is ast.Pow and (type(left) not in numbers or type(right) not in numbers or abs(right) > 64):
            raise _Halt('value_budget')
        if kind is ast.Mult:
            for sequence, count in ((left, right), (right, left)):
                if type(sequence) in (str, list, tuple) and type(count) in (int, bool) and count > 0:
                    unit = self.measure(sequence) - (0 if type(sequence) is str else 1)
                    if unit * count + (0 if type(sequence) is str else 1) > SIZE_LIMIT:
                        raise _Halt('value_budget')
        if kind is ast.Mod and type(left) is str:
            raise _Halt('construct')
        operation = BINARY.get(kind)
        if operation is None:
            raise _Halt('construct')
        return self.check(operation(left, right))

    def loop(self, node, scopes):
        iterator = self.iterate(self.expr(node.iter, scopes)) if type(node) is ast.For else None
        while True:
            self.tick()
            if iterator is not None:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                self.assign(node.target, item, scopes)
            elif not self.expr(node.test, scopes):
                break
            try:
                self.run(node.body, scopes)
            except _Break:
                return
            except _Continue:
                continue
        self.run(node.orelse, scopes)

    def throw(self, node, scopes):
        if node.exc is None:
            if not self.handling:
                raise RuntimeError('no active exception to re-raise')
            raise self.handling[-1]
        error = self.expr(node.exc, scopes)
        if isinstance(error, type) and issubclass(error, Exception):
            error = error()
        if not isinstance(error, Exception):
            raise TypeError('exceptions must derive from Exception')
        raise error

    def attempt(self, node, scopes):
        """try/except/else/finally where a resource stop is final.

        Ordinary exceptions and return/break/continue signals still run the
        finally block (which may replace them, as in Python). A `_Halt` from the
        body, a handler, else or a nested try skips every remaining candidate
        block, so `finally: return 7` cannot turn a refused allocation into a value.
        """
        try:
            try:
                self.run(node.body, scopes)
            except Exception as error:
                self.handle(node, error, scopes)
            else:
                self.run(node.orelse, scopes)
        except _Halt:
            raise
        except BaseException:
            self.run(node.finalbody, scopes)
            raise
        self.run(node.finalbody, scopes)

    def handle(self, node, error, scopes):
        # Interpreter-internal resource failures are stops, never candidate-catchable errors.
        if isinstance(error, MemoryError):
            raise _Halt('value_budget') from None
        if isinstance(error, RecursionError):
            raise _Halt('call_depth') from None
        for handler in node.handlers:
            expected = Exception if handler.type is None else self.expr(handler.type, scopes)
            classes = expected if isinstance(expected, tuple) else (expected,)
            if not all(isinstance(item, type) and issubclass(item, Exception) for item in classes):
                raise TypeError('catching classes that do not inherit from Exception is not allowed')
            if isinstance(error, classes):
                if handler.name:
                    scopes[-1][handler.name] = error
                self.handling.append(error)
                try:
                    self.run(handler.body, scopes)
                finally:
                    self.handling.pop()  # Host bookkeeping only; no candidate code runs here.
                return
        raise error


def interpret_checks(source, function, inputs):
    """Grading-child entry: interpret the candidate once per input.

    Receives only check names and inputs, never expected values. Returns
    bounded result metadata; the host decides pass/fail.
    """
    if static_gate(source.encode('utf-8'), function) is not None:
        return [{'name': item['name'], 'status': 'halted', 'reason': 'static_gate', 'mutated': False} for item in inputs]
    tree = ast.parse(source)
    results = []
    for item in inputs:
        original = list(item['args'])
        argument = tuple(original) if item.get('tuple') else list(original)
        record = {'name': item['name'], 'status': 'returned', 'type': None, 'value': None}
        try:
            interpreter = _Interpreter(tree)
            target = interpreter.globals.get(function)
            if type(target) is not _Function:
                raise _Halt('function_missing')
            value = interpreter.invoke(target, [argument], {})
            known = (int, float, bool, str, list, tuple, type(None))
            record['type'] = type(value).__name__ if type(value) in known else 'other'
            if type(value) is float and _math.isfinite(value) or type(value) is int and value.bit_length() <= 64:
                record['value'] = value
        except _Halt as halt:
            reason = halt.args[0] if halt.args and halt.args[0] in HALT_REASONS else 'construct'
            record.update(status='halted', reason=reason)
        except MemoryError:
            record.update(status='halted', reason='value_budget')
        except RecursionError:
            record.update(status='halted', reason='call_depth')
        except Exception as error:
            record.update(status='raised', value_error=isinstance(error, ValueError))
        except BaseException:  # A stray control signal is a construct failure.
            record.update(status='halted', reason='construct')
        record['mutated'] = len(argument) != len(original) or any(a is not b and a != b
                                                                  for a, b in zip(argument, original))
        results.append(record)
    return results
