from __future__ import annotations

import argparse

from bianbu_agent.cli.output import emit
from bianbu_agent.home import AgentHome


def plugin_member_hint(package_id: str, kind: str) -> str:
    """Explain a qualified Plugin member ID to the resource CLI.

    Plugin-contributed members are addressed as ``<plugin>/<local-name>`` in the
    Inventory, but they live inside the Plugin package, not in the data
    directory a file resource command reads.
    """

    plugin_name = str(package_id).split("/", 1)[0]
    return (
        f"{package_id} is a Plugin-contributed {kind}; "
        f"read it with `bianbu-agent plugin members` (owner: {plugin_name})"
    )


def package_command(
    home: AgentHome,
    args: argparse.Namespace,
    *,
    runtime_config: object | None = None,
) -> int:
    from importlib import import_module

    if args.resource == "skill" and args.action in {"show", "verify", "remove"}:
        package_id = str(args.package_id)
        if "/" in package_id:
            raise ValueError(plugin_member_hint(package_id, "Skill"))
    modules = {"skill": "skills", "plugin": "plugins"}
    manager_type = getattr(
        import_module(f"bianbu_agent.{modules[args.resource]}"),
        f"{args.resource.title()}PackageManager",
    )
    manager = manager_type(home, file_only=True)
    if args.action == "effective":
        configs = getattr(runtime_config, "plugin_configs", {})
        value = manager.effective(dict(configs))
    elif args.action == "rollback":
        value = manager.rollback(args.package_id)
    elif args.action == "list":
        # Plugin listing reports unreadable packages on stderr and keeps the
        # rest of the catalog: one bad directory must not hide every Plugin.
        value = manager.scan()[0] if args.resource == "plugin" else manager.list()
    elif args.action == "install":
        value = manager.install(
            args.source,
            upgrade=bool(getattr(args, "upgrade", False)),
        ) if args.resource == "plugin" else manager.install(args.source)
    elif args.action == "show":
        value = manager.inspect(args.package_id)
    elif args.action == "verify":
        value = {"valid": manager.verify(args.package_id)}
    elif args.action == "members":
        # An optional Plugin name narrows the list to one namespace; without
        # it the whole installed catalog is reported.
        selected = getattr(args, "package_id", None)
        value = tuple(
            member
            for member in manager.list_members()
            if selected is None or member.namespace == selected
        )
    else:
        deleted = manager.uninstall(args.package_id)
        value = {"deleted": deleted}
    emit(value)
    return 0
