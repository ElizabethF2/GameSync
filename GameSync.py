import os, sys, fnmatch, json, shutil, time, subprocess, tempfile
from datetime import datetime
sys.path.append('..')
import path_helper

NAS_PATH = 'Liz/Projects/GameSync'
LOCAL_STATE_PATH = os.path.expandvars('%LOCALAPPDATA%/GameSync/local_state.json')
REMOTE_STATE_PATH = 'remote_state.json'
LOCK_FILE_PATH = 'lock_file'
USE_LOCK_FILE = True

try:
    from os import scandir
except ImportError:
    from scandir import scandir  # use scandir PyPI module on Python < 3.5

class FileLock(object):
  def __init__(self):
    if USE_LOCK_FILE:
      while True:
        try: 
          self.fd = open(LOCK_FILE_PATH, 'x')
          break
        except FileExistsError:
          time.sleep(1)
  
  def __enter__(self):
    return self

  def __exit__(self, *e):
    if USE_LOCK_FILE:
      self.fd.close()
      os.remove(self.fd.name)
    
def scantree(path):
    for entry in scandir(path):
      if entry.is_dir(follow_symlinks=False):
        for entry in scantree(entry.path):
          yield entry
      else:
        yield entry

_7z_path = None
def get_7z_path():
  global _7z_path
  if _7z_path is not None:
    return _7z_path
  for p in ('%ProgramW6432%', '%ProgramFiles%', '%ProgramFiles(x86)%'): 
    p = os.path.join(os.path.expandvars(p), '7-Zip\\7z.exe')
    if os.path.exists(p):
      return p
  return '7z.exe'

def parse_games_list():
  with open('games.txt','r') as f:
    games = {}
    for line in f:
      s = line.split(':')
      try:
        arg = s[0].lower()
        value = ':'.join(s[1:]).rstrip()
        if value[0] == ' ':
          value = value[1:]
        if arg == 'name':
          current_game = value
        elif arg == 'save path':
          game = games.setdefault(current_game, {})
          game.setdefault('paths',[]).append(value)
        elif arg == 'include':
          game = games.setdefault(current_game, {})
          game.setdefault('include',[]).append(value)
        elif arg == 'exclude':
          game = games.setdefault(current_game, {})
          game.setdefault('exclude',[]).append(value)  
        elif arg == 'game path':
          game = games.setdefault(current_game, {})
          game['game path'] = value
      except IndexError:
        pass
    for game in games.values():
      game.setdefault('paths',[])
      game.setdefault('exclude',[])
      inc = game.setdefault('include',[])
      if not inc:
        inc.append('*')
    return games

def get_local_files_for_game(game):
  files = set()
  for path in game['paths']:
    local_path = os.path.expandvars(path)
    if local_path[-1] != os.path.sep:
      local_path += os.path.sep
    try:
      for entry in scantree(local_path):
        if entry.is_file():
          should_include = False
          for i in game['include']:
            if fnmatch.fnmatch(entry.path, i):
              should_include = True
          for e in game['exclude']:
            if fnmatch.fnmatch(entry.path, i):
              should_include = False
          if should_include:
            relpath = os.path.relpath(entry.path, local_path)
            files.add((path, entry.path, relpath, entry.stat().st_mtime))
    except FileNotFoundError:
      pass
  if len(files) != len(set((r for _,_,r,_ in files))):
    raise ValueError('Multiple files have the same remote path. Try changing your path to a higher directory.', game)
  return files

def main():
  print('Starting GameSync...')
  print('Acquiring lock...')
  with FileLock():
    games = parse_games_list()
    try: os.makedirs(os.path.dirname(LOCAL_STATE_PATH))
    except FileExistsError: pass
    try:
      with open(LOCAL_STATE_PATH,'r') as f:
       local_state = json.loads(f.read())
    except FileNotFoundError:
      local_state = {}
    local_state_changed = False
    try:
      with open(REMOTE_STATE_PATH,'r') as f:
       remote_state = json.loads(f.read())
    except FileNotFoundError:
      remote_state = {}
    remote_state_changed = False
    for game_name, game in games.items():
      print('Syncing %s...' % (game_name,))

      # Get the local and remote state of files
      local_files = get_local_files_for_game(game)
      last_local_update = int(max((m for _,_,_,m in local_files), default=0))
      local_game_state = local_state.setdefault(game_name, {'last_update':0})
      last_uploaded_update = int(local_game_state['last_update'])
      remote_files = remote_state.setdefault(game_name, {'files':{}})['files']
      last_remote_update = int(max((i['mtime'] for i in remote_files.values()), default=0))

      try:
        game_path = os.path.expandvars(game['game path'])
      except KeyError:
        game_path = None

      # Figure out which action to perform and prompt the user if there's a conflict
      action = 'skip'
      if len(local_files) < 1 and game_path and os.path.exists(game_path):
        action = 'pull'
      elif len(local_files) > 0 and last_local_update == last_uploaded_update and last_remote_update > last_local_update:
        action = 'pull'
      elif last_local_update > last_uploaded_update and last_remote_update == last_uploaded_update:
        action = 'push'
      elif last_uploaded_update != last_local_update and last_uploaded_update != last_remote_update:
        print('  A save has already been synced from another device. What would you like to do?')
        print('  Last Remote Change:', datetime.utcfromtimestamp(last_remote_update).strftime('%Y-%m-%d %H:%M:%S'))
        print('  Last Local Change:', datetime.utcfromtimestamp(last_local_update).strftime('%Y-%m-%d %H:%M:%S'))
        i = input('  keep (l)ocal save, keep (r)emote save, (s)kip? ')
        if i == 'l':
          action = 'push'
        elif i == 'r':
          action = 'pull'

      # Figure out which files need to be transferred
      # Also creates local directories and deletes local files
      files_to_copy = set()
      if action == 'skip':
        print('  Nothing to sync, skipped.')
      elif action == 'pull':
        print('  Pulling remote changes...')
        local_files_by_name = {rel:{'full_path':full,'mtime':mtime} for root,full,rel,mtime  in local_files}
        deleted_files = set((r for _,_,r,_ in local_files)) - set(remote_files.keys())
        for file in deleted_files:
          os.remove(local_files_by_name[file]['full_path'])
        for remote_relpath, remote_file in remote_files.items():
          should_download = False
          try:
            if remote_file['mtime'] > local_files_by_name[remote_relpath]['mtime']:
              should_download = True
              os.remove(local_files_by_name[remote_relpath]['full_path'])
          except KeyError:
            should_download = True
          if should_download:
            local_path = os.path.expandvars(os.path.join(remote_file['root'], remote_relpath))
            try: os.makedirs(os.path.dirname(local_path))
            except FileExistsError: pass
            files_to_copy.add(remote_relpath)
        local_game_state['last_update'] = last_remote_update
        local_state_changed = True
      elif action == 'push':
        print('  Pushing local changes...')
        chunk = max((f['chunk'] for f in remote_files.values()), default=-1) + 1
        deleted_files = set((remote_files.keys())) - set((r for _,_,r,_ in local_files))
        for file in deleted_files:
          remote_path = os.path.join(game_name, file)
          remote_files.pop(file)
        for root, local_path, local_relpath, local_mtime in local_files:
          should_upload = False
          remote_path = os.path.join(game_name, local_relpath)
          try:
            if local_mtime > remote_files[local_relpath]['mtime']:
              should_upload = True
          except KeyError:
            should_upload = True
          if should_upload:
            files_to_copy.add(local_relpath)
            remote_files[local_relpath] = {'root': root, 'mtime': local_mtime, 'chunk': chunk}
        local_game_state['last_update'] = last_local_update
        local_state_changed = True
        remote_state_changed = True

      if action == 'pull':  # Download chunks and extract files from chunks
        if len(files_to_copy) > 0:
          # Group files by chunk
          remote_files_to_copy_grouped_by_chunks = {}
          for relpath in files_to_copy:
            f = remote_files[relpath]
            remote_files_to_copy_grouped_by_chunks.setdefault(f['chunk'], {})[relpath] = f
        
        nas_root = os.path.join(path_helper.get_path(), NAS_PATH)
        game_root = os.path.join(nas_root, game_name)
        for chunk, files in remote_files_to_copy_grouped_by_chunks.items():
          # Extract chunk archive
          temp_dir = tempfile.mkdtemp()
          arc_name = 'chunk'+str(chunk)+'.7z'
          local_arc_path = os.path.join(temp_dir, arc_name)
          remote_arc_path = os.path.join(game_root, arc_name)
          shutil.copy2(remote_arc_path, local_arc_path)
          print('_'*50)
          subprocess.run([get_7z_path(), 'x', local_arc_path, '-o'+temp_dir])
          print('_'*50)

          # Move files to the correct local path
          for relpath, metadata in files.items():
            extracted_path = os.path.join(temp_dir, relpath)
            local_path = os.path.join(os.expandvars(metadata['root']), relpath)
            os.rename(extracted_path, local_path)
          
          shutil.rmtree(temp_dir)

      elif action == 'push':  # Compress files in chunks and upload chunks
        if len(files_to_copy) > 0:
          # Compress files and upload to NAS
          arc_name = 'chunk'+str(chunk)+'.7z'
          nas_root = os.path.join(path_helper.get_path(), NAS_PATH)
          game_root = os.path.join(nas_root, game_name)
          remote_arc_path = os.path.join(game_root, arc_name)
          temp_dir = tempfile.mkdtemp()
          file_list_path = os.path.join(temp_dir, 'file_list.txt')
          local_arc_path = os.path.join(temp_dir, arc_name)

          # Group files by root
          files_to_copy_grouped_by_root = {}
          for relpath in files_to_copy:
            root = remote_files[relpath]['root']
            files_to_copy_grouped_by_root.setdefault(root, set()).add(relpath)

          # Add files to the archive
          for root, files in files_to_copy_grouped_by_root.items():
            with open(file_list_path, 'w') as f:
              f.write('\n'.join(files))
            root = os.path.expandvars(root)
            print('_'*50)
            subprocess.run([get_7z_path(), 'a', local_arc_path, '-mhe', '-mx=9', '-scsWIN', '@'+file_list_path], cwd=root)
            print('_'*50)

          # Ensure a folder exists on the NAS for the game
          try: os.makedirs(game_root)
          except (FileExistsError, PermissionError): pass

          # Copy the archive to the NAS
          shutil.copy2(local_arc_path, remote_arc_path)
          shutil.rmtree(temp_dir)

          # List chunks on the NAS
          unused_chunks = set()
          for fname in os.listdir(game_root):
            unused_chunks.add(int(fname[5:-3]))

          # Remove chunks still being used from list
          for v in remote_files.values():
            try: unused_chunks.remove(v['chunk'])
            except KeyError: pass

          # Delete unused chunks
          for c in unused_chunks:
            os.remove(os.path.join(game_root, 'chunk'+str(c)+'.7z'))

    if local_state_changed:
      with open(LOCAL_STATE_PATH, 'w') as f:
        f.write(json.dumps(local_state))
    if remote_state_changed:
      with open(REMOTE_STATE_PATH, 'w') as f:
        f.write(json.dumps(remote_state))
  print('Done!')

if __name__ == '__main__':
  main()
