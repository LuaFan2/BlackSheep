import re
import logging
from abc import abstractmethod
from collections import defaultdict
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, AnyStr
from urllib.parse import unquote

from blacksheep import HttpMethod
from blacksheep.utils import ensure_bytes, ensure_str

__all__ = [
    "Router",
    "Route",
    "RouteMatch",
    "RouteDuplicate",
    "RegisteredRoute",
    "RoutesRegistry",
]


_route_all_rx = re.compile(b"\\*")
_route_param_rx = re.compile(b"/:([^/]+)")
_named_group_rx = re.compile(b"\\?P<([^>]+)>")
_escaped_chars = {b".", b"[", b"]", b"(", b")"}


def _get_regex_for_pattern(pattern):

    for c in _escaped_chars:
        if c in pattern:
            pattern = pattern.replace(c, b"\\" + c)
    if b"*" in pattern:
        # throw exception if a star appears more than once
        if pattern.count(b"*") > 1:
            raise ValueError(
                "A route pattern cannot contain more than one star sign *. "
                "Multiple star signs are not supported."
            )

        if b"/*" in pattern:
            pattern = _route_all_rx.sub(br"?(?P<tail>.*)", pattern)
        else:
            pattern = _route_all_rx.sub(br"(?P<tail>.*)", pattern)
    if b"/:" in pattern:
        pattern = _route_param_rx.sub(br"/(?P<\1>[^\/]+)", pattern)

    # NB: following code is just to throw user friendly errors;
    # regex would fail anyway, but with a more complex message
    # 'sre_constants.error: redefinition of group name'
    # we only return param names as they are useful for other things
    param_names = []
    for p in _named_group_rx.finditer(pattern):
        param_name = p.group(1)
        if param_name in param_names:
            raise ValueError(
                f"cannot have multiple parameters with name: " f"{param_name}"
            )

        param_names.append(param_name)

    if len(pattern) > 1 and not pattern.endswith(b"*"):
        # NB: the /? at the end, ensures that a route is matched both with
        # a trailing slash or not
        pattern = pattern + b"/?"
    return re.compile(b"^" + pattern + b"$", re.IGNORECASE), param_names


class RouteException(Exception):
    ...


class RouteDuplicate(RouteException):
    def __init__(self, method, pattern, current_handler):
        method = ensure_str(method)
        pattern = ensure_str(pattern)
        super().__init__(
            f"Cannot register route pattern `{pattern}` for "
            f"`{method}` more than once. "
            f"This pattern is already registered for handler "
            f"{current_handler.__qualname__}."
        )
        self.method = method
        self.pattern = pattern
        self.current_handler = current_handler


class RouteMatch:

    __slots__ = ("values", "handler")

    def __init__(self, route: "Route", values: Optional[Dict[str, bytes]]):
        self.handler = route.handler
        self.values: Optional[Dict[str, str]] = (
            {k: unquote(v.decode("utf8")) for k, v in values.items()}
            if values
            else None
        )


class Route:

    __slots__ = ("handler", "pattern", "has_params", "param_names", "_rx")

    pattern: bytes

    def __init__(self, pattern: AnyStr, handler: Any):
        if isinstance(pattern, str):
            raw_pattern = pattern.encode("utf8")
        else:
            raw_pattern = pattern
        if raw_pattern == b"":
            raw_pattern = b"/"
        if len(raw_pattern) > 1 and raw_pattern.endswith(b"/"):
            raw_pattern = raw_pattern.rstrip(b"/")
        raw_pattern = raw_pattern.lower()
        self.handler = handler
        self.pattern = raw_pattern
        self.has_params = b"*" in raw_pattern or b":" in raw_pattern
        rx, param_names = _get_regex_for_pattern(raw_pattern)
        self._rx = rx
        self.param_names = [name.decode("utf8") for name in param_names]

    @property
    def full_pattern(self) -> bytes:
        return self._rx.pattern

    def match(self, value: bytes) -> Optional[RouteMatch]:
        if not self.has_params and value.lower() == self.pattern:
            return RouteMatch(self, None)

        match = self._rx.match(value)

        if not match:
            return None

        return RouteMatch(self, match.groupdict() if self.has_params else None)


class RouterBase:
    @abstractmethod
    def add(self, method: str, pattern: str, handler: Callable) -> None:
        """Adds a request handler for the given HTTP method and route pattern."""

    def mark_handler(self, handler: Callable) -> None:
        setattr(handler, "route_handler", True)

    def normalize_default_pattern_name(self, handler_name: str) -> str:
        return handler_name.replace("_", "-")

    def get_decorator(
        self, method: str, pattern: Optional[str] = "/"
    ) -> Callable[..., Any]:
        def decorator(fn):
            nonlocal pattern
            if pattern is ... or pattern is None:
                # default to something depending on decorated function's name
                if fn.__name__ in {"index", "default"}:
                    pattern = "/"
                else:
                    pattern = "/" + self.normalize_default_pattern_name(fn.__name__)

                logging.debug(
                    "Defaulting to route pattern '%s' for" "request handler <%s>",
                    pattern,
                    fn.__qualname__,
                )
            self.add(method, pattern, fn)
            return fn

        return decorator

    def add_head(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.HEAD, pattern, handler)

    def add_get(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.GET, pattern, handler)

    def add_post(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.POST, pattern, handler)

    def add_put(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.PUT, pattern, handler)

    def add_delete(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.DELETE, pattern, handler)

    def add_trace(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.TRACE, pattern, handler)

    def add_options(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.OPTIONS, pattern, handler)

    def add_connect(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.CONNECT, pattern, handler)

    def add_patch(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add(HttpMethod.PATCH, pattern, handler)

    def head(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.HEAD, pattern)

    def get(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.GET, pattern)

    def post(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.POST, pattern)

    def put(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.PUT, pattern)

    def delete(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.DELETE, pattern)

    def trace(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.TRACE, pattern)

    def options(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.OPTIONS, pattern)

    def connect(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.CONNECT, pattern)

    def patch(self, pattern: Optional[str] = "/") -> Callable[..., Any]:
        return self.get_decorator(HttpMethod.PATCH, pattern)


class Router(RouterBase):

    __slots__ = ("routes", "_map", "_fallback")

    def __init__(self):
        self._map = {}
        self._fallback = None
        self.routes: Dict[bytes, List[Route]] = defaultdict(list)

    @property
    def fallback(self):
        return self._fallback

    @fallback.setter
    def fallback(self, value):
        if not isinstance(value, Route):
            if callable(value):
                self._fallback = Route(b"*", value)
                return
            raise ValueError("fallback must be a Route or a callable")
        self._fallback = value

    def __iter__(self):
        for key, routes in self.routes.items():
            for route in routes:
                yield route
        if self._fallback:
            yield self._fallback

    def _is_route_configured(self, method: bytes, route: Route):
        method_patterns = self._map.get(method)
        if not method_patterns:
            return False
        if method_patterns.get(route.full_pattern):
            return True
        return False

    def _set_configured_route(self, method: bytes, route: Route):
        method_patterns = self._map.get(method)
        if not method_patterns:
            self._map[method] = {route.full_pattern: route}
        else:
            method_patterns[route.full_pattern] = route

    def _check_duplicate(self, method: bytes, new_route: Route):
        if self._is_route_configured(method, new_route):
            current_route = self._map.get(method).get(new_route.full_pattern)
            raise RouteDuplicate(method, new_route.pattern, current_route.handler)
        self._set_configured_route(method, new_route)

    def add(self, method: str, pattern: AnyStr, handler: Any):
        self.mark_handler(handler)
        method_name = ensure_bytes(method)
        new_route = Route(ensure_bytes(pattern), handler)
        self._check_duplicate(method_name, new_route)
        self.add_route(method_name, new_route)

    def add_route(self, method: AnyStr, route: Route):
        self.routes[ensure_bytes(method)].append(route)

    def sort_routes(self):
        """
        Sorts the current routes in order of dynamic parameters ascending.
        The catch-all route is always placed to the end.
        """
        current_routes = self.routes.copy()

        for method in current_routes.keys():
            current_routes[method].sort(key=lambda route: len(route.param_names))
            current_routes[method].sort(key=lambda route: b"*" in route.pattern)
            current_routes[method].sort(key=lambda route: b"*" == route.pattern)

        self.routes = current_routes

    @lru_cache(maxsize=1200)
    def get_match(self, method: AnyStr, value: AnyStr) -> Optional[RouteMatch]:
        for route in self.routes[ensure_bytes(method)]:
            match = route.match(ensure_bytes(value))
            if match:
                return match
        if self._fallback is None:
            return None

        return RouteMatch(self._fallback, None)


class RegisteredRoute:

    __slots__ = ("method", "pattern", "handler")

    def __init__(self, method: str, pattern: str, handler: Callable):
        self.method = method
        self.pattern = pattern
        self.handler = handler


class RoutesRegistry(RouterBase):
    """
    A registry for routes: not a full router able to get matches.
    Unlike a router, a registry does not throw for duplicated routes;
    because such routes can be modified when applied to an actual router.

    This class is meant to enable scenarios like base pattern for controllers.
    """

    def __init__(self):
        self.routes: List[RegisteredRoute] = []

    def __iter__(self):
        yield from self.routes

    def add(self, method: str, pattern: str, handler: Callable):
        self.mark_handler(handler)
        self.routes.append(RegisteredRoute(method, pattern, handler))
