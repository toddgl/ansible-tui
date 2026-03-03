import tempfile
import os
import asyncio
import sys
import yaml
from pathlib import Path
from pathlib import Path
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Tree, Static, Input, Log, Select, Button
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual import events
from textual.screen import ModalScreen

import subprocess
import json
from pathlib import Path


class AnsibleProject:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

        self.inventory_path = self.detect_inventory()   

    # -------------------------
    # Inventory Detection
    # -------------------------

    def detect_inventory(self) -> Path:
        """Locate inventory like Ansible does."""

        # 1️⃣ ansible.cfg override
        cfg = self.root / "ansible.cfg"
        if cfg.exists():
            for line in cfg.read_text().splitlines():
                line = line.strip()
                if line.startswith("inventory"):
                    _, value = line.split("=", 1)
                    candidate = (self.root / value.strip()).resolve()
                    if candidate.exists():
                        return candidate

        # 2️⃣ common filenames
        candidates = [
            "hosts",
            "inventory",
            "inventory.yml",
            "inventory.yaml",
        ]

        for name in candidates:
            path = self.root / name
            if path.exists():
                return path

        # 3️⃣ inventory directory
        inv_dir = self.root / "inventory"
        if inv_dir.exists():
            return inv_dir

        raise RuntimeError("No Ansible inventory found")

    # -------------------------
    # Load Inventory via ansible
    # -------------------------

    def load_inventory(self, vault_password: str | None = None):

        cmd = [
            "ansible-inventory",
            "-i",
            str(self.inventory_path),
            "--list",
        ]

        temp_path = None

        try:
            if vault_password:

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    delete=False,
                    prefix="ansible_vault_",
                ) as f:
                    f.write(vault_password + "\n")
                    temp_path = f.name

                os.chmod(temp_path, 0o600)

                cmd.extend(["--vault-password-file", temp_path])

            print("RUNNING:", " ".join(cmd))  # keep temporarily

            result = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.root,
            )

            if result.returncode != 0:
                raise RuntimeError(result.stderr)

            return json.loads(result.stdout)

        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    # -------------------------
    # Detect Playbooks
    # -------------------------
    def detect_playbooks(self) -> list[Path]:
        return sorted(self.root.glob("*.yml"))

    # -------------------------
    # Detect Roles
    # -------------------------
    def detect_roles(self) -> list[str]:
        roles_path = self.root / "roles"
        if not roles_path.exists():
            return []

        return sorted([p.name for p in roles_path.iterdir() if p.is_dir()])

    # -------------------------
    # Handle projects with a vault
    # -------------------------
    def project_uses_vault(self) -> bool:
        for path in self.root.rglob("*.yml"):
            try:
                if "$ANSIBLE_VAULT" in path.read_text(errors="ignore"):
                    return True
            except Exception:
                pass
        return False


# -------------------------
# Command Builder
# -------------------------

class CommandBuilder:
    def __init__(self, project: AnsibleProject):
        self.project = project
        self.playbook: Path | None = None
        self.hosts: set[str] = set()
        self.roles: set[str] = set()
        self.check = False
        self.diff = False

    def build(self) -> str:
        if not self.playbook:
            return "No playbook selected"

        inventory = self.project.detect_inventory()
        cmd = f"ansible-playbook -i {inventory} {self.playbook.name}"

        if self.hosts:
            cmd += f" --limit {','.join(sorted(self.hosts))}"

        if self.roles:
            cmd += f" --tags {','.join(sorted(self.roles))}"

        if self.check:
            cmd += " --check"

        if self.diff:
            cmd += " --diff"

        return cmd


# -------------------------
# Vault Password Modal
# -------------------------


class VaultModal(ModalScreen[str]):

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-dialog"):
            yield Static("Enter Ansible Vault Password")
            yield Input(
                placeholder="Vault password",
                password=True,
                id="vault_input",
            )
            yield Button("OK", id="ok")

    def on_mount(self) -> None:
        # Ensure typing works immediately
        self.query_one("#vault_input", Input).focus()

    # ✅ ENTER key handler
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    # ✅ Button click handler
    def on_button_pressed(self, event: Button.Pressed) -> None:
        password = self.query_one("#vault_input", Input).value
        self.dismiss(password)

    # ✅ Optional ESC cancel
    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss("")


# -------------------------
# Main Application
# -------------------------

class AnsibleTUI(App):

    CSS_PATH = "ansible-tui.tccs"

    BINDINGS = [
        ("tab", "focus_next", "Switch"),
        ("enter", "submit", "Submit"),
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

    def __init__(self, project_path: Path, **kwargs):
        super().__init__(**kwargs)
        
        self.vault_password: str | None = None
       
        self.project = AnsibleProject(project_path)
        self.command_builder = CommandBuilder(self.project)

        self.inventory_data = {}

    # -------------------------
    # Suspend textual App then call load inventory
    # -------------------------

    def _load_inventory_blocking(self):
        return self.project.load_inventory(
            vault_password=self.vault_password
        )

    # -------------------------
    # UI Construction
    # -------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        # ---- TOP CONTROL AREA ----
        with Horizontal(id="main"):

            # Hosts
            with Vertical():
                yield Tree("Hosts", id="hosts")

            # Roles
            with Vertical():
                yield  Tree("Roles", id="roles")

            # Playbook selector
            self.playbook_select = Select(
                [],
                prompt="Select Playbook",
                id="playbooks",
            )
            with Vertical():
                yield self.playbook_select

        # ---- COMMAND PREVIEW ----
        self.preview_widget = Static("", id="preview")
        yield self.preview_widget

        # ------LOGS ---
        self.output_log = Log(id="output")
        yield self.output_log

        yield Footer()

    @property
    def host_tree(self) -> Tree:
        return self.query_one("#hosts", Tree)

    @property
    def role_tree(self) -> Tree:
        return self.query_one("#roles", Tree)

    @property
    def preview(self) -> Static:
        return self.query_one("#preview", Static)

    # -------------------------
    # Load the playbooks
    # -------------------------

    def load_playbooks(self):
        playbooks = self.project.detect_playbooks()

        options = [(p.name, p.name) for p in playbooks]

        self.playbook_select.set_options(options)

        if playbooks:
            self.command_builder.playbook = playbooks[0]
            self.playbook_select.value = playbooks[0].name
            self.update_preview()
    
    # -------------------------
    # React to selections
    # -------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "playbooks":
            return

        self.command_builder.playbook = (
            self.project.root / event.value
        )

        self.update_preview()

    # -------------------------
    # Startup
    # -------------------------
    
    async def on_mount(self):
        self.run_worker(
            self.load_project_worker,
            exclusive=True,
            name="project-loader",
        )

    # -------------------------
    # Load Project Worker
    # -------------------------
    
    async def load_project_worker(self):

        # --- vault ---
        if self.project.project_uses_vault():
            password = await self.push_screen_wait(VaultModal())
            if not password:
                return
            self.vault_password = password

        # --- load inventory safely ---
        print("Before Load")
        self.inventory_data = await asyncio.to_thread(
            self._load_inventory_blocking
        )
        print("After Load")
        # --- update UI (now safe) ---
        self.populate_inventory_tree()
        self.load_roles()
        self.load_playbooks()
        self.update_preview()


    # -------------------------
    # UI Update
    # -------------------------

    def _project_loaded(self, inventory):
        """Back on UI thread."""

        self.inventory_data = inventory

        self.populate_inventory_tree()
        self.load_roles()
        self.load_playbooks()
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
    #  Populate inventorty Tree
    # -------------------------
    def populate_inventory_tree(self):

        tree = self.host_tree
        tree.clear()

        root = tree.root
        root.label = "Hosts"
        root.expand()

        data = self.inventory_data

        # groups listed under "all"
        groups = data.get("all", {}).get("children", [])

        for group_name in groups:

            group_node = root.add(
                group_name,
                data={
                    "type": "group",
                    "name": group_name,
                    "checked": False,
                },
            )

            group = data.get(group_name, {})
            hosts = group.get("hosts", [])

            for host in hosts:
                group_node.add(
                    f"[ ] {host}",
                    data={
                        "type": "host",
                        "name": host,
                        "checked": False,
                    },
                )

            group_node.expand()

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
        roles_path = self.project.root / "roles"

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
        password = await self.push_screen_wait(VaultModal())
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
    # Submit
    # -------------------------
    def action_submit(self):
        password = self.query_one(Input).value
        self.dismiss(password)

    # -------------------------
    # Command Preview
    # -------------------------
    
    def update_preview(self):
        self.command_builder.hosts = set(self.selected_hosts)
        self.command_builder.roles = set(self.selected_roles)
        self.command_builder.check = self.check_mode
        self.command_builder.diff = self.diff_mode

        cmd = self.command_builder.build()

        if self.vault_password:
            cmd += " --ask-vault-pass"

        self.current_command = cmd
        self.preview.update(cmd)


    # -------------------------
    # Run Playbook
    # -------------------------
    async def action_run_playbook(self):
        cmd = self.command_builder.build()

        self.output_log.clear()
        self.output_log.write_line(f"Running: {cmd}")

        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=self.project.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async for line in process.stdout:
            self.output_log.write_line(line.decode().rstrip())

        await process.wait()
        self.output_log.write_line("✓ Run complete")


if __name__ == "__main__":
    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    AnsibleTUI(project_dir).run()
