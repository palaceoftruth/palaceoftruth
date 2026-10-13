FROM scratch

# Ship every module the package imports at runtime. A prior image copied only
# __init__.py + plugin.yaml, so /plugin failed to import with
# "No module named 'plugin.checkpoint'".
COPY __init__.py checkpoint.py outbox.py credentials.py cli.py /plugin/
COPY plugin.yaml /plugin/plugin.yaml
