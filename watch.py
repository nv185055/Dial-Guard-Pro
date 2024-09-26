import os
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from subprocess import Popen, PIPE

class RestartOnChangesHandler(FileSystemEventHandler):
    def __init__(self, restart_function):
        self.restart_function = restart_function

    def on_any_event(self, event):
        if event.event_type in ['modified', 'created', 'deleted']:
            print(f"Detected change in: {event.src_path}. Restarting...")
            self.restart_function()

class AppManager:
    def __init__(self, script_name):
        self.process = None
        self.script_name = script_name

    def start_app(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
        print(f"Starting script: {self.script_name}")
        self.process = Popen(["python", self.script_name])

    def stop_app(self):
        if self.process:
            self.process.terminate()
            self.process.wait()

def monitor_directory(directory, app_manager):
    event_handler = RestartOnChangesHandler(app_manager.start_app)
    observer = Observer()
    observer.schedule(event_handler, directory, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    script_name = "atlan.py"  # Replace with the name of your main script
    app_manager = AppManager(script_name)

    # Start the app for the first time
    app_manager.start_app()

    # Monitor the current directory for changes
    monitor_directory(os.getcwd(), app_manager)
