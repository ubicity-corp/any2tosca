import yaml
from pathlib import Path

class ToscaWriter:
    @staticmethod
    def write(node_dict: dict, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)

        for name, content in node_dict.items():
            path = output_dir / f"{name.replace('.', '_')}.yaml"
            with open(path, "w") as f:
                yaml.safe_dump({name: content}, f, sort_keys=False)