"""Evaluate only restricted pure Python functions, never arbitrary model scripts.

This is an evaluation fixture, not a general coding execution sandbox.
"""
from __future__ import annotations

import ast
import json
import resource
import sys


def evaluate(request: dict) -> dict:
    code = request["code"]
    tree = ast.parse(code)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != request["function"]:
        raise ValueError("Expected exactly one named function.")
    if any(not isinstance(n, ast.FunctionDef) for n in tree.body):
        raise ValueError("Only a function definition is accepted at module scope.")
    if functions[0].decorator_list:
        raise ValueError("Decorators are not accepted.")
    forbidden = (ast.Import, ast.ImportFrom, ast.Attribute, ast.ClassDef,
                 ast.AsyncFunctionDef, ast.Global, ast.Nonlocal, ast.With,
                 ast.AsyncWith, ast.Lambda, ast.Await, ast.Yield, ast.YieldFrom)
    permitted_calls = {"min", "max", "len", "range", "enumerate", "sum", "abs",
                       "sorted", "reversed", "int", "float", "str", "list",
                       "tuple", "bool", "ValueError"}
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ValueError(f"Unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and "__" in node.id:
            raise ValueError("Dunder names are not accepted.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in permitted_calls:
                raise ValueError("Only whitelisted pure builtin calls are accepted.")
    safe = {name: getattr(__import__("builtins"), name) for name in permitted_calls}
    namespace = {"__builtins__": safe}
    exec(compile(tree, "<restricted-model-fixture>", "exec"), namespace)
    function = namespace[request["function"]]
    outcomes = []
    for case in request["cases"]:
        try:
            actual = function(*case["args"])
            passed = actual == case.get("expected") and not case.get("raises")
        except ValueError:
            actual = "ValueError"
            passed = case.get("raises") == "ValueError"
        outcomes.append({"passed": bool(passed), "actual": actual})
    return {"passed": all(case["passed"] for case in outcomes), "cases": outcomes}


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    # macOS memory limits are not portable. These fixtures accept only pure
    # functions over tiny controlled inputs; this is not a general sandbox.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        print(json.dumps(evaluate(json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps({"passed": False, "error": str(error)}))
