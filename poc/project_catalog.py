"""Read the native Codex project hierarchy without reading chat histories."""
import json
from pathlib import Path
import sqlite3
import uuid

from codex_probe import connect_read_only, find_database


class ProjectCatalog:
    def __init__(self, workspace_roots, include_desktop=False, codex_home=None):
        self.configured_roots = [str(Path(p).resolve()) for p in workspace_roots]
        self.include_desktop = include_desktop
        self.home = Path(codex_home or Path.home() / '.codex')

    def projects(self):
        projects = []
        if self.include_desktop:
            db = connect_read_only(find_database(self.home, 'state'))
            try:
                rows = db.execute('SELECT p.id,p.name,r.path FROM projects p JOIN project_roots r ON r.project_id=p.id ORDER BY p.position,r.position').fetchall()
            finally:
                db.close()
            by_id = {}
            for row in rows:
                if not Path(row['path']).is_absolute():
                    continue
                if row['id'] not in by_id:
                    project = {'id': row['id'], 'name': row['name'], 'roots': [], 'source': 'native Codex project catalog'}
                    by_id[row['id']] = project
                    projects.append(project)
                by_id[row['id']]['roots'].append(str(Path(row['path']).resolve()))
        native_roots = {root for project in projects for root in project['roots']}
        for root in self.configured_roots:
            if root not in native_roots:
                projects.append({'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'airemote-project:' + root)),
                    'name': Path(root).name, 'roots': [root], 'source': 'configured workspace'})
        for project in projects:
            project['path'] = project['roots'][0]
        return projects

    def project(self, project_id):
        project_id = str(uuid.UUID(project_id))
        project = next((p for p in self.projects() if p['id'] == project_id), None)
        if project is None:
            raise LookupError('Unknown project')
        return project

    def legacy_assignments(self):
        if not self.include_desktop:
            return {}
        try:
            state = json.loads((self.home / '.codex-global-state.json').read_text())
        except (OSError, ValueError):
            return {}
        mapping = state.get('app-server-project-id-by-legacy-project-id-by-host', {}).get('local:' + str(self.home), {})
        return {thread_id: mapping.get(value.get('projectId'), value.get('projectId'))
                for thread_id, value in state.get('thread-project-assignments', {}).items()
                if isinstance(value, dict) and value.get('projectKind') == 'local'}

    @staticmethod
    def project_for_row(row, projects, assignments=None):
        assigned = row.get('project_id')
        if assigned and any(project['id'] == assigned for project in projects):
            return assigned
        legacy = (assignments or {}).get(row['id'])
        if legacy and any(project['id'] == legacy for project in projects):
            return legacy
        cwd = Path(row['cwd']).resolve()
        matches = [(len(Path(root).parts), project['id']) for project in projects for root in project['roots']
                   if cwd.is_relative_to(Path(root))]
        return max(matches)[1] if matches else None

    def metadata(self, thread_id):
        db = connect_read_only(find_database(self.home, 'state'))
        try:
            row = db.execute('SELECT id,name,title,cwd,project_id,history_mode,archived,updated_at FROM threads WHERE id=?',
                             (str(uuid.UUID(thread_id)),)).fetchone()
        finally:
            db.close()
        if row is None:
            raise LookupError('Unknown chat')
        return dict(row)

    def project_for_thread(self, thread_id):
        return self.project_for_row(self.metadata(thread_id), self.projects(), self.legacy_assignments())

    def threads(self, project_id, allowed_ids=None):
        projects = self.projects()
        project = next((p for p in projects if p['id'] == str(uuid.UUID(project_id))), None)
        if project is None:
            raise LookupError('Unknown project')
        conditions, values = ['project_id=?'], [project['id']]
        assignments = self.legacy_assignments()
        assigned_here = [thread_id for thread_id, assigned in assignments.items() if assigned == project['id']]
        if assigned_here:
            conditions.append('id IN (' + ','.join('?' for _ in assigned_here) + ')')
            values.extend(assigned_here)
        for root in project['roots']:
            escaped = root.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.extend(['cwd=?', "cwd LIKE ? ESCAPE '\\'"])
            values.extend([root, escaped.rstrip('/') + '/%'])
        db = connect_read_only(find_database(self.home, 'state'))
        try:
            # Titles and grouping only. No history DB, message text, runtime IPC,
            # artifact scan, or first_user_message/preview field is read here.
            rows = db.execute('SELECT id,name,title,cwd,project_id,history_mode,archived,updated_at FROM threads '
                              'WHERE archived=0 AND (' + ' OR '.join(conditions) + ') ORDER BY updated_at DESC,id DESC', values).fetchall()
        finally:
            db.close()
        result = []
        for record in rows:
            row = dict(record)
            if allowed_ids is not None and row['id'] not in allowed_ids:
                continue
            if self.project_for_row(row, projects, assignments) != project['id']:
                continue
            result.append({'threadId': row['id'], 'title': row['name'] or row['title'], 'cwd': row['cwd'],
                'projectId': project['id'], 'historyMode': row['history_mode'], 'updatedAt': row['updated_at'],
                'runtimeStatus': 'unknown', 'runtimeSource': 'metadata only; open chat for live status',
                'latestPersistedTurnStatus': None, 'turns': []})
        return result

    def attach_turn_status(self, threads):
        """Read only latest-turn metadata for these already expanded chat IDs."""
        if not threads:
            return threads
        try:
            db = connect_read_only(find_database(self.home, 'thread_history'))
        except (FileNotFoundError, sqlite3.Error):
            return threads
        try:
            for thread in threads:
                row = db.execute('SELECT status FROM thread_turns WHERE thread_id=? '
                                 'ORDER BY rollout_ordinal DESC LIMIT 1', (thread['threadId'],)).fetchone()
                if row is None:
                    continue
                status = row['status']
                thread.update(latestPersistedTurnStatus=status,
                    runtimeStatus='active' if status in {'inProgress', 'in_progress', 'running'} else 'idle',
                    runtimeSource='latest native turn metadata')
        except sqlite3.Error:
            pass
        finally:
            db.close()
        return threads
