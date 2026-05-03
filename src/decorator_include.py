"""Minimal bundled implementation of django-decorator-include.

Replaces the external django-decorator-include package so no pip install
is needed during the Docker build.
"""

from django.urls import include
from django.urls.resolvers import URLPattern, URLResolver


def decorator_include(decorator, arg, namespace=None):
    """Wrap a URLconf include with a decorator applied to all view functions."""
    decorators = [decorator] if callable(decorator) else list(decorator)
    urlconf_module, app_name, namespace = include(arg, namespace=namespace)

    def _decorate_patterns(patterns):
        result = []
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                result.append(
                    URLResolver(
                        pattern.pattern,
                        _DecoratedURLconf(pattern.urlconf_module),
                        pattern.default_kwargs,
                        pattern.app_name,
                        pattern.namespace,
                    )
                )
            elif isinstance(pattern, URLPattern):
                callback = pattern.callback
                for dec in reversed(decorators):
                    callback = dec(callback)
                result.append(URLPattern(pattern.pattern, callback, pattern.default_args, pattern.name))
        return result

    class _DecoratedURLconf:
        def __init__(self, urlconf):
            self._urlconf = urlconf

        @property
        def urlpatterns(self):
            patterns = getattr(self._urlconf, "urlpatterns", self._urlconf)
            return _decorate_patterns(patterns)

    return _DecoratedURLconf(urlconf_module), app_name, namespace
