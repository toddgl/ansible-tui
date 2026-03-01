import asyncio
import sys
import yaml
from pathlib import Path
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Tree, Static, Input
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual import events
from textual.widgets import Log


# -------------------------
# Vault Password Modal
# -------------------------

class VaultModal(ModalScreen[str]):
    """Simple vault password prompt."""

    def compose(self) -> ComposeResult:
        yield Static("Enter Vault Password:", id="vault_label")
        self.input = Input(password=True)
        yield self.input

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self.input.value)


# -------------------------
# Main App
# -------------------------

class AnsibleTUI(App):

    CSS_PATH = "ansible-tui.tccs"

    BINDINGS = [
        ("tab", "focus_next", "Switch"),
        ("space", "toggle", "Toggle"),
        ("a", "all_hosts", "All Hosts"),
        ("r", "run_playbook", "Run"),
        ("v", "vault_prompt", "Vault"),
        ("c", "toggle_check", "Check"),
        ("d", "toggle_diff", "Diff"),
        ("q", "quit", "Quit"),
    ]

    selected_hosts = reactive(set)
    selected_roles = reactive(set)
    vault_password = reactive("")
    check_mode = reactive(False)
    diff_mode = reactive(False)

    current_command = reactive("")
    run_output = reactive("") 


    # -------------------------
    # Accept a working directory on startup
    # -------------------------

    def __init__(self, project_path: Path | None = None, **kwargs):
        super().__init__(**kwargs)
        self.project_path = Path(project_path).resolve() if project_path else Path.cwd()

    # -------------------------
    # UI Construction
    # -------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="main"):
            self.host_tree = Tree("Hosts", id="hosts")
            self.role_tree = Tree("Roles", id="roles")

            yield self.host_tree
            yield self.role_tree

        self.preview = Static("", id="preview")
        self.output_log = Log(id="output")

        yield self.preview
        yield self.output_log
        yield Footer()

    # -------------------------
    # Startup
    # -------------------------

    async def on_mount(self) -> None:
        self.load_inventory()
        self.load_roles()
        self.update_preview()

    # -------------------------
    # Node Creation Helper
    # -------------------------

    def create_node(self, parent, name, node_type):
        node = parent.add(
            name,
            data={
                "type": node_type,
                "name": name,
                "checked": False,
            },
        )
        self.refresh_node(node)
        return node

    # -------------------------
    # Inventory Loading (Clean)
    # -------------------------

    def load_inventory(self):
        inventory_file = self.project_path / "inventory.yml"

        if not inventory_file.exists():
            data = {
                "all": {
                    "children": {
                        "webservers": {
                            "hosts": {
                                "web01": {},
                                "web02": {},
                            }
                        },
                        "dbservers": {
                            "hosts": {
                             "db01": {}
                            }
                        },
                    }
                }
            }
        else:
            data = yaml.safe_load(inventory_file.read_text())

        root = self.host_tree.root
        root.expand()

        for group, content in data["all"]["children"].items():
            group_node = self.create_node(root, group, "group")

            for host in content.get("hosts", {}):
                self.create_node(group_node, host, "host")

    # -------------------------
    # Render Function
    # -------------------------

    def render_node_label(self, node):
        if not node.data:
            return node.label

        checked = node.data.get("checked", False)
        name = node.data["name"]

        checkbox = "[green][x][/green]" if checked else "[dim][ ][/dim]"
        
        return f"{checkbox} {name}"


    # -------------------------
    # Tree Refresh Helper
    # -------------------------

    def refresh_node(self, node):
        node.set_label(self.render_node_label(node))

    # -------------------------
    # Recursive Walk Helper
    # -------------------------

    def walk_tree(self, node):
        yield node
        for child in node.children:
            yield from self.walk_tree(child)

    # -------------------------
    # Role Loading
    # -------------------------

    def load_roles(self):
        roles_path = self.project_path / "roles"

        root = self.role_tree.root
        root.expand()

        if roles_path.exists():
            for role_dir in roles_path.iterdir():
                if role_dir.is_dir():
                    self.create_node(root, role_dir.name, "role")
        else:
            # Demo roles
            for role in ["apt", "deploy", "nginx"]:
                self.create_node(root, role, "role")

    # -------------------------
    # Toggle Selection
    # -------------------------

    async def action_toggle(self):
        tree = self.focused
        if not isinstance(tree, Tree):
            return

        node = tree.cursor_node
        if not node or not node.data:
            return

        name = node.data["name"]

        new_state = not node.data.get("checked", False)
        self.set_checked_recursive(node, new_state)
        self.update_selected_sets()

        self.update_preview()

    # -------------------------
    # Recursive State Propogation
    # -------------------------

    def set_checked_recursive(self, node, state):
        if node.data:
            node.data["checked"] = state
            self.refresh_node(node)

        for child in node.children:
            self.set_checked_recursive(child, state)

    # -------------------------
    # Keep Selected Sets in Sync
    # -------------------------
    
    def update_selected_sets(self):
        self.selected_hosts.clear()
        self.selected_roles.clear()

        for node in self.walk_tree(self.host_tree.root):
            if (
                node.data
                and node.data.get("type") == "host"
                and node.data.get("checked", False)
            ):
                self.selected_hosts.add(node.data["name"])

        for node in self.walk_tree(self.role_tree.root):
            if (
                node.data
                and node.data.get("type") == "role"
                and node.data.get("checked", False)
            ):
                self.selected_roles.add(node.data["name"])

    # -------------------------
    # Select All Hosts
    # -------------------------

    async def action_all_hosts(self):
        for node in self.walk_tree(self.host_tree.root):
            if node.data and node.data["type"] == "host":
                node.data["checked"] =True
                self.refresh_node(node)

        self.update_selected_sets()
        self.update_preview()

    # -------------------------
    # Vault Prompt
    # -------------------------

    async def action_vault_prompt(self):
        password = await self.push_screen(VaultModal())
        if password:
            self.vault_password = password
        self.update_preview()

    # -------------------------
    # Toggle Flags
    # -------------------------

    async def action_toggle_check(self):
        self.check_mode = not self.check_mode
        self.update_preview()

    async def action_toggle_diff(self):
        self.diff_mode = not self.diff_mode
        self.update_preview()

    # -------------------------
    # Command Preview
    # -------------------------
    
    def update_preview(self):
        playbook = self.project_path / "playbook.yml"
        inventory = self.project_path / "inventory.yml"

        cmd = f"ansible-playbook -i {inventory} {playbook}"

        if self.selected_hosts:
            cmd += f" --limit {','.join(self.selected_hosts)}"

        if self.selected_roles:
            cmd += f" --tags {','.join(self.selected_roles)}"

        if self.check_mode:
            cmd += " --check"

        if self.diff_mode:
            cmd += " --diff"

        if self.vault_password:
            cmd += " --ask-vault-pass"

        # Store command in app state
        self.current_command = cmd

        # Display in preview
        self.preview.update(cmd)

    # -------------------------
    # Run Playbook
    # -------------------------
    async def action_run_playbook(self):
        cmd = self.current_command
        self.output_log.clear()
        self.output_log.write_line(f"Running: {cmd}")

        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=self.project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        async for line in process.stdout:
            self.output_log.write_line(line.decode().rstrip())

        await process.wait()
        self.output_log.write_line("✓ Run complete")


if __name__ == "__main__":
    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    AnsibleTUI(project_dir).run()
