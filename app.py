import shutil 
import tempfile
import os
import asyncio
import sys
import yaml
from pathlib import Path
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Tree, Static, Input, RichLog, Select, Button, LoadingIndicator
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual import events
from textual.screen import ModalScreen

import subprocess
import json


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
        # Ensure we use the correct FreeBSD path
        ansible_bin = "/usr/local/bin/ansible-inventory"
        if not os.path.exists(ansible_bin):
            import shutil
            ansible_bin = shutil.which("ansible-inventory") or "ansible-inventory"

        cmd = [
            ansible_bin,
            "-i", str(self.inventory_path),
            "--list",
        ]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Prevent Ansible from ever trying to prompt the terminal
        env["ANSIBLE_ASK_VAULT_PASS"] = "False" 

        password_file = None
        try:
            if vault_password:
                # Create a temporary file for the password
                with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                    f.write(vault_password)
                    password_file = f.name
                cmd.extend(["--vault-password-file", password_file])

            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL, # Tell Ansible there is no keyboard input
                timeout=30
            )

            if result.returncode != 0:
                return {"error": result.stderr}

            return json.loads(result.stdout)

        finally:
            # Always clean up the password file
            if password_file and os.path.exists(password_file):
                os.remove(password_file)
    

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
        # Avoid scanning hidden directories like .git or .venv
        for path in self.root.rglob("*.yml"):
            if any(part.startswith('.') for part in path.parts):
                continue
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
        self.become = False
        self.run_as_root = False

    def build(self) -> list[str]:
        if not self.playbook:
            return []

        inventory = self.project.detect_inventory()

        parts = [
            "ansible-playbook",
            "-i",
            str(inventory),
            str(self.playbook),
        ]

        if self.hosts:
            parts.extend([
                "--limit",
                ",".join(sorted(self.hosts)),
            ])

        if self.roles:
            parts.extend([
                "--tags",
                ",".join(sorted(self.roles)),
            ])

        if self.check:
            parts.append("--check")

        if self.diff:
            parts.append("--diff")

        if self.become:
            parts.append("--become")

        if self.run_as_root:
            parts.insert(0, "sudo")

        return parts

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
# Become Password Modal
# -------------------------

class BecomePasswordModal(ModalScreen[str]):

    def compose(self) -> ComposeResult:
        with Vertical(id="become-dialog"):
            yield Static("Enter sudo password")
            yield Input(
                placeholder="sudo password",
                password=True,
                id="become_input",
            )
            yield Button("OK", id="ok")

    def on_mount(self) -> None:
        self.query_one("#become_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        password = self.query_one("#become_input", Input).value
        self.dismiss(password)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss("")

# -------------------------
# Sudoo Become Password Modal
# -------------------------

class SudoPasswordModal(ModalScreen[str]):

    def compose(self) -> ComposeResult:
        with Vertical(id="sudo-dialog"):
            yield Static("Enter local sudo password")
            yield Input(
                placeholder="sudo password",
                password=True,
                id="sudo_input",
            )
            yield Button("OK", id="ok")

    def on_mount(self) -> None:
        self.query_one("#sudo_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        password = self.query_one("#sudo_input", Input).value
        self.dismiss(password)

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
        ("b", "toggle_become", "Become"),
        ("s", "toggle_sudo", "Sudo"),
        ("q", "quit", "Quit"),
    ]

    selected_hosts = reactive(set)
    selected_roles = reactive(set)
    vault_password = reactive("")
    check_mode = reactive(False)
    diff_mode = reactive(False)
    become_enabled = reactive(False)
    run_as_root = reactive(False)

    current_command = reactive(list)
    run_output = reactive("") 


    # -------------------------
    # Accept a working directory on startup
    # -------------------------

    def __init__(self, project_path: Path, **kwargs):
        super().__init__(**kwargs)
        
        self.vault_password = None
        self.vault_become = None
        self.sudo_password = None    
       
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

        with Horizontal(id="main"):
            with Vertical():
                yield Tree("Inventory", id="inventory_tree")
            with Vertical():
                yield Tree("Roles", id="roles")
            
            self.playbook_select = Select([], prompt="Select Playbook", id="playbooks")
            with Vertical():
                yield self.playbook_select

        # --- NEW STATUS BAR ---
        with Horizontal(id="status-bar"):
            yield Static("FLAGS:", id="status-label")
            yield Static("[CHECK OFF]", id="status-check")
            yield Static("[DIFF OFF]", id="status-diff")
            yield Static("[VAULT OFF]", id="status-vault")
            yield Static("[BECOME OFF]", id="status-become")
            yield Static("[SUDO OFF]", id="status-sudo")

        self.preview_widget = Static("", id="preview")
        yield self.preview_widget

        self.output_log = RichLog(id="output", highlight=True, markup=True)
        yield self.output_log

        yield Footer()


    @property
    def host_tree(self) -> Tree:
        return self.query_one("#inventory_tree", Tree)

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
        # 1. Start the Spinner
        spinner = LoadingIndicator()
        await self.mount(spinner)
    
        try:
            self.output_log.write("Checking for Vault...")
            has_vault = await asyncio.to_thread(self.project.project_uses_vault)

            if has_vault:
                # We must remove the spinner before pushing the Modal, 
                # or the spinner might block the password input.
                await spinner.remove() 
                self.vault_password = await self.push_screen_wait(VaultModal())
                # Re-mount spinner for the actual inventory load
                spinner = LoadingIndicator()
                await self.mount(spinner)

            self.output_log.write("Querying Vultr inventory...")
        
            # Run the blocking inventory call in a thread
            self.inventory_data = await asyncio.to_thread(
                self.project.load_inventory, 
                self.vault_password
            )

            if "error" in self.inventory_data:
                self.output_log.write(f"[red]Error:[/red] {self.inventory_data['error']}")
                return

            # 2. Populate UI with the new data
            self.populate_inventory_tree()
            self.load_roles()
            self.load_playbooks()
            self.output_log.write(f"[green]✓ Load complete.[/green]")

        except Exception as e:
            self.output_log.write(f"[red]Unexpected error: {e}[/red]")
        finally:
            # 3. Always ensure the spinner is removed
            try:
                await spinner.remove()
            except:
                pass
   
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
        tree = self.query_one("#inventory_tree", Tree)
        root = tree.root
        root.expand()

        for group, data in self.inventory_data.items():
            if group == "_meta": continue
            # Groups are also nodes that can be checked/unchecked
            group_node = root.add(
                f"[bold cyan]{group}[/bold cyan]", 
                data={"type": "group", "name": group, "checked": False},
                expand=True
            )
            
            hosts = data.get("hosts", [])
            for host in hosts:
                # IMPORTANT: Data must be a dictionary, not just a string
                group_node.add_leaf(
                    f"☐ {host}", 
                    data={"type": "host", "name": host, "checked": False}
                )

    # -------------------------
    # Render Function
    # -------------------------

    def render_node_label(self, node):
        if not node.data or "checked" not in node.data:
            return node.label

        checked = node.data.get("checked", False)
        name = node.data["name"]
        # Use the Unicode symbols as requested
        checkbox = "☑" if checked else "☐"
        
        return f"{checkbox} {name}"




    # -------------------------
    # Tree Refresh Helper
    # -------------------------

    def refresh_node(self, node):
        node.label = self.render_node_label(node)

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
    # Node selection event handling
    # -------------------------
    
    def on_tree_node_selected(self, event: Tree.NodeSelected):
        """Handles Mouse Clicks and Enter Key."""
        self.toggle_node_state(event.node)

    async def action_toggle(self):
        """Handles Spacebar via BINDINGS."""
        if isinstance(self.focused, Tree):
            if self.focused.cursor_node:
                self.toggle_node_state(self.focused.cursor_node)

    def toggle_node_state(self, node):
        """Unified logic to change check state and update UI."""
        if node.data and "checked" in node.data:
            node.data["checked"] = not node.data["checked"]
            self.refresh_node(node)
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
            if node.data and node.data.get("type") == "host":
                node.data["checked"] = True
                self.refresh_node(node)

        self.update_selected_sets()
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

    def action_toggle_become(self):
        self.run_worker(
            self.become_password_worker,
            exclusive=True,
            name="become-password",
        )

    async def become_password_worker(self):
        # Turning Become OFF
        if self.become_enabled:
            self.become_enabled = False
            self.become_password = None
            self.update_preview()
            return

        # Become is being turned ON
        self.run_as_root = False

        password = await self.push_screen_wait(
            BecomePasswordModal()
        )

        # User cancelled
        if not password:
            return

        self.become_password = password
        self.become_enabled = True

        self.update_preview()

    def action_toggle_sudo(self):
        self.run_worker(
            self.sudo_password_worker,
            exclusive=True,
            name="sudo-password",
        )

    async def sudo_password_worker(self):

        # Turning sudo OFF
        if self.run_as_root:
            self.run_as_root = False
            self.sudo_password = None
            self.update_preview()
            return

        # Turning sudo ON
        self.become_enabled = False

        password = await self.push_screen_wait(
            SudoPasswordModal()
        )

        # User cancelled
        if not password:
            return

        self.sudo_password = password
        self.run_as_root = True

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

        # Synchronise CommandBuilder with UI state
        self.command_builder.hosts = self.selected_hosts
        self.command_builder.roles = self.selected_roles
        self.command_builder.check = self.check_mode
        self.command_builder.diff = self.diff_mode
        self.command_builder.become = self.become_enabled
        self.command_builder.run_as_root = self.run_as_root

        # Build command
        cmd = self.command_builder.build()

        # Vault is handled at execution time.
        # Don't put a fake password path into the preview.
        self.current_command = cmd

        # Update status indicators
        check_widget = self.query_one("#status-check", Static)
        diff_widget = self.query_one("#status-diff", Static)
        vault_widget = self.query_one("#status-vault", Static)
        become_widget = self.query_one("#status-become", Static)
        sudo_widget = self.query_one("#status-sudo", Static)

        if self.check_mode:
            check_widget.update("[bold cyan]✓ CHECK ON[/]")
        else:
            check_widget.update("[dim]  CHECK OFF[/]")

        if self.diff_mode:
            diff_widget.update("[bold yellow]✓ DIFF ON[/]")
        else:
            diff_widget.update("[dim]  DIFF OFF[/]")

        if self.vault_password:
            vault_widget.update("[bold green]🔒 VAULT LOADED[/]")
        else:
            vault_widget.update("[dim]🔓 VAULT EMPTY[/]")

        if self.become_enabled:
            become_widget.update("[bold red]✓ BECOME ON[/]")
        else:
            become_widget.update("[dim]  BECOME OFF[/]")

        if self.run_as_root:
            sudo_widget.update("[bold red]✓ SUDO ON[/]")
        else:
            sudo_widget.update("[dim]  SUDO OFF[/]")    

        # Command preview
        preview = " ".join(cmd)

        self.query_one("#preview", Static).update(
            f"[bold green]{preview}[/bold green]"
        )

    # -------------------------
    # Run Playbook
    # -------------------------

    async def action_run_playbook(self):

        if not self.current_command:
            return

        log = self.query_one("#output", RichLog)
        log.clear()

        cmd = list(self.current_command)

        # Temporary credential files
        pass_file = None
        become_temp_file = None

        # -------------------------------------------------
        # Local sudo
        # -------------------------------------------------

        if self.run_as_root:
            cmd = [
                "sudo",
                "-S",
                "-p",
                "",
            ] + cmd[1:]

        try:

            # -------------------------------------------------
            # Ansible Become password
            # -------------------------------------------------

            if self.become_enabled and self.become_password:

                fd, become_temp_file = tempfile.mkstemp(
                    prefix="ansible_become_"
                )

                try:
                    os.write(
                        fd,
                        (self.become_password + "\n").encode()
                    )
                finally:
                    os.close(fd)

                os.chmod(become_temp_file, 0o600)

                cmd.extend([
                    "--become-password-file",
                    become_temp_file,
                ])

            # -------------------------------------------------
            # Ansible Vault password
            # -------------------------------------------------

            if self.vault_password:

                fd, pass_file = tempfile.mkstemp(
                    prefix="ansible_vault_"
                )

                try:
                    os.write(
                        fd,
                        (self.vault_password + "\n").encode()
                    )
                finally:
                    os.close(fd)

                os.chmod(pass_file, 0o600)

                cmd.extend([
                    "--vault-password-file",
                    pass_file,
                ])

            # -------------------------------------------------
            # Display command
            # -------------------------------------------------

            display_cmd = " ".join(cmd)

            log.write(
                "[bold cyan]Executing:[/bold cyan]\n"
                    f"{display_cmd}\n"
            )

            # -------------------------------------------------
            # Run Ansible
            # -------------------------------------------------

            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.project.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
            )

            # -------------------------------------------------
            # Supply local sudo password
            # -------------------------------------------------

            if self.run_as_root and self.sudo_password:

                process.stdin.write(
                    (self.sudo_password + "\n").encode()
                )

                await process.stdin.drain()

            if process.stdin:
                process.stdin.close()

            # -------------------------------------------------
            # Read output
            # -------------------------------------------------

            async for line in process.stdout:

                text = line.decode(
                    errors="replace"
                ).rstrip()

                log.write(text)

            return_code = await process.wait()

            # -------------------------------------------------
            # Result
            # -------------------------------------------------

            if return_code == 0:

                log.write(
                    "\n[bold green]"
                        "✓ Playbook completed successfully."
                        "[/bold green]"
                )

            else:

                log.write(
                    f"\n[bold red]"
                        f"✗ Playbook failed "
                        f"(exit code {return_code})."
                        f"[/bold red]"
                )

        except Exception as exc:

            log.write(
                f"\n[bold red]"
                    f"Runner error: {exc}"
                    f"[/bold red]"
            )

        finally:

            # -------------------------------------------------
            # Remove temporary credential files
            # -------------------------------------------------

            if become_temp_file and os.path.exists(become_temp_file):
                try:
                    os.remove(become_temp_file)
                except OSError:
                    pass

            if pass_file and os.path.exists(pass_file):
                try:
                    os.remove(pass_file)
                except OSError:
                    pass


if __name__ == "__main__":
    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    AnsibleTUI(project_dir).run()
