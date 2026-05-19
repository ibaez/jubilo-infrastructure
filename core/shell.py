import subprocess

def run(cmd, capture_output=False, process_env=None):
    if capture_output:
        # --- CAPTURE MODE (no streaming) ---
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            env=process_env
        )

        if result.returncode != 0:
            raise Exception(
                f"Command failed: {cmd}\nError: {result.stderr}"
            )

        return result.stdout.strip()

    else:
        # --- STREAMING MODE ---
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=process_env
        )

        # stream live
        for line in process.stdout:
            print(line, end="")

        process.wait()

        if process.returncode != 0:
            raise Exception(f"Command failed: {cmd}")

        return None