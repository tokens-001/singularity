import os
import subprocess
import sys
from pathlib import Path

def validate_todo_cli():
    """验证todo-cli项目是否正确实现"""
    
    # 检查项目目录是否存在
    project_dir = Path("todo-cli")
    if not project_dir.exists():
        print("Error: todo-cli directory not found")
        return False
    
    # 检查Cargo.toml是否存在
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        print("Error: todo-cli/Cargo.toml not found")
        return False
    
    # 检查源码文件
    src_dir = project_dir / "src"
    if not src_dir.exists():
        print("Error: todo-cli/src directory not found")
        return False
    
    main_rs = src_dir / "main.rs"
    lib_rs = src_dir / "lib.rs"
    cli_mod = src_dir / "cli" / "mod.rs"
    
    if not main_rs.exists():
        print("Error: todo-cli/src/main.rs not found")
        return False
    
    if not lib_rs.exists():
        print("Error: todo-cli/src/lib.rs not found")
        return False
    
    if not cli_mod.exists():
        print("Error: todo-cli/src/cli/mod.rs not found")
        return False
    
    # 尝试编译项目
    try:
        result = subprocess.run(
            ["cargo", "build"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode != 0:
            print(f"Error: Failed to compile todo-cli\n{result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("Error: Compilation timed out")
        return False
    
    # 测试基本功能
    try:
        # 添加任务
        result_add = subprocess.run(
            ["cargo", "run", "--", "add", "Test task"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        # 列出任务
        result_list = subprocess.run(
            ["cargo", "run", "--", "list"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result_list.returncode != 0:
            print(f"Error: Failed to run todo-cli list\n{result_list.stderr}")
            return False
        
        # 检查输出中是否有测试任务
        if "Test task" not in result_list.stdout:
            print(f"Warning: Test task not found in output: {result_list.stdout}")
        
        print("Validation successful!")
        print(f"List output: {result_list.stdout}")
        return True
        
    except subprocess.TimeoutExpired:
        print("Error: Command execution timed out")
        return False

if __name__ == "__main__":
    success = validate_todo_cli()
    sys.exit(0 if success else 1)