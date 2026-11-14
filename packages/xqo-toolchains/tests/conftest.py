import os

os.environ.setdefault("XQO_ATTENTION_TIMEOUT", "0.2")  # auto-confirm quickly in tests

from xqo_toolchains import ToolchainsPlugin  # noqa: E402

ToolchainsPlugin.register()  # what the plugin manager does at startup

try:  # the docker tests need xqo-docker; without it they are skipped
    import xqo_docker  # noqa: E402
    xqo_docker.DockerPlugin.register()
except ImportError:
    xqo_docker = None
