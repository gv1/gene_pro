__rcs_id__ = "$Id: genealogy.py,v 1.216 2026/06/24 12:09:53 george Exp $"

import wx
import wx.lib.scrolledpanel
import wx.grid as gridlib
import wx.dataview as dv
import os
import json
import csv
import wx.lib.agw.customtreectrl as CT
import hashlib
import time
import sys
import argparse
import datetime
import re
import shutil
import base64
import urllib.request
import matplotlib.pyplot as plt
import networkx as nx

debug = 0

def dtime():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

def dprint(*args):
    if debug > 1:
        print(*args)
        
class GridCellAutoCompleteEditor(wx.grid.GridCellTextEditor):
    def __init__(self, choices):
        super().__init__()
        self.choices = choices

    def Create(self, parent, id, evtHandler):
        super().Create(parent, id, evtHandler)
        text_ctrl = self.GetControl()
        if isinstance(text_ctrl, wx.TextCtrl):
            text_ctrl.AutoComplete(self.choices)


class GenealogyIO:
    def __init__(self, db_instance):
        self.db = db_instance 

    def repair_broken_links(self):
        """Manually force-links people to families based on nickname strings."""
        if self.db.read_only:
            return 0
        self.db.cursor.execute("""
            UPDATE people 
            SET family_id = (
                SELECT family_id FROM families 
                WHERE families.family_name = people.family_name
            )
            WHERE family_name IS NOT NULL AND family_name != '';
        """)
        self.db.conn.commit()
        
        self.db.cursor.execute("SELECT COUNT(*) FROM people WHERE family_id IS NOT NULL")
        count = self.db.cursor.fetchone()[0]
        return count    
        
    def ensure_columns_exist(self, cursor, conn, data_dict):
        """Checks if keys in data_dict exist as columns in the 'people' table."""
        if self.db.read_only:
            return
        cursor.execute("PRAGMA table_info(people)")
        existing_cols = [row[1] for row in cursor.fetchall()]
        
        for key in data_dict.keys():
            if key not in existing_cols and key != 'id':
                cursor.execute(f"ALTER TABLE people ADD COLUMN {key} TEXT")
                conn.commit()
                
    def _write_csv(self, file_path, cursor):
        """Helper to write cursor data to a CSV file."""
        rows = cursor.fetchall()
        colnames = [d[0] for d in cursor.description]
        
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(colnames)
            writer.writerows(rows)

    def export_subsegment_data(self, filepath, target_group=None, target_family=None):
        """Filters database parameters to slice out a specific family segment and all its sub-branches."""
        import sqlite3 as plain_sqlite
        import networkx as nx
        
        query_conditions = []
        query_params = []
        
        # 1. Gather ALL sub-families tracking down from this branch node
        target_families_manifest = set()
        if target_family:
            target_families_manifest.add(target_family.lower().strip())
            
        try:
            # Build a local NetworkX graph of family structural links to discover sub-branches
            self.db.cursor.execute("SELECT family_name, ancestral_family_name, family_group FROM families")
            all_fam_records = self.db.cursor.fetchall()
            
            G_fam = nx.DiGraph()
            for f_name, a_name, f_grp in all_fam_records:
                fn = str(f_name).strip().lower() if f_name else ""
                an = str(a_name).strip().lower() if a_name else ""
                fg = str(f_grp).strip().lower() if f_grp else ""
                
                if fn:
                    G_fam.add_node(fn, group=fg)
                    # If an ancestral link exists, map the directional tree hierarchy
                    if an and an != fn:
                        G_fam.add_edge(an, fn)
                    elif fg and fg != fn:
                        G_fam.add_edge(fg, fn)

            # If tracking a specific family node, collect all its downstream tree descendants
            if target_family and target_family.lower().strip() in G_fam:
                root_node = target_family.lower().strip()
                descendants = nx.descendants(G_fam, root_node)
                for d in descendants:
                    target_families_manifest.add(d)
            
            # If tracking a broad group, collect everything inside that ecosystem cluster
            if target_group and not target_family:
                tg_lower = target_group.lower().strip()
                for node, data in G_fam.nodes(data=True):
                    if data.get('group') == tg_lower or node == tg_lower:
                        target_families_manifest.add(node)
                        descendants = nx.descendants(G_fam, node)
                        target_families_manifest.update(descendants)
                        
        except Exception as e:
            return False, f"Family graph tracking pass failed: {str(e)}"

        # 2. Extract matching records from the FAMILIES table using our manifest
        try:
            if target_family and not target_group:
                # Slicing a strict specific sub-branch path
                placeholders = ", ".join(["?"] * len(target_families_manifest))
                self.db.cursor.execute(f"SELECT * FROM families WHERE LOWER(TRIM(family_name)) IN ({placeholders})", list(target_families_manifest))
            else:
                # Broad group capture filtering
                self.db.cursor.execute("SELECT * FROM families WHERE LOWER(TRIM(family_group)) = LOWER(TRIM(?))", (target_group,))
                
            family_rows = self.db.cursor.fetchall()
            self.db.cursor.execute("PRAGMA table_info(families)")
            family_cols = [c[1] for c in self.db.cursor.fetchall()]
        except Exception as e:
            return False, f"Families data harvesting failed: {str(e)}"

        # 3. Extract PEOPLE matching any criteria (Active family, Ancestral link, or Group root)
        try:
            p_rows_map = {}
            self.db.cursor.execute("SELECT * FROM people")
            all_people = self.db.fetchall() if hasattr(self.db, 'fetchall') else self.db.cursor.fetchall()
            self.db.cursor.execute("PRAGMA table_info(people)")
            people_cols = [c[1] for c in self.db.cursor.fetchall()]
            
            # Map column indices to look up names accurately
            col_map = {col: idx for idx, col in enumerate(people_cols)}
            
            for p_row in all_people:
                p_fam = str(p_row[col_map['family_name']]).strip().lower() if p_row[col_map['family_name']] else ""
                p_anc = str(p_row[col_map['ancestral_family_name']]).strip().lower() if 'ancestral_family_name' in col_map and p_row[col_map['ancestral_family_name']] else ""
                p_grp = str(p_row[col_map['family_group']]).strip().lower() if 'family_group' in col_map and p_row[col_map['family_group']] else ""
                
                match = False
                # Scenario A: Target parameters watch an explicit family subset chain
                if target_families_manifest:
                    if p_fam in target_families_manifest or p_anc in target_families_manifest:
                        match = True
                # Scenario B: Target watches a broad parental group bucket
                elif target_group:
                    if p_grp == target_group.lower().strip() or p_fam == target_group.lower().strip():
                        match = True
                        
                if match:
                    p_rows_map[p_row[0]] = p_row # Keyed by unique item ID row index
            
            people_rows = list(p_rows_map.values())
        except Exception as e:
            return False, f"People lineage collection failed: {str(e)}"

        # --- FIX: Define low_path right here before branching into outputs ---
        low_path = filepath.lower()

        # 4. Handle Alternate File Format Interceptions (Strictly Isolated Return)
        if low_path.endswith('.json'):
            try:
                export_payload = {
                    "metadata": {
                        "scope": target_family or target_group, 
                        "generated_ist": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
                    },
                    "families": [dict(zip(family_cols, r)) for r in family_rows],
                    "people": [dict(zip(people_cols, r)) for r in people_rows]
                }
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(export_payload, f, indent=4)
                return True, f"Successfully extracted {len(people_rows)} individuals to JSON file."
            except Exception as e:
                return False, f"JSON generation failed: {str(e)}"

        # 5. Route default output down to unencrypted clean SQLite3 Database Structure
        try:
            final_path = filepath if low_path.endswith('.db') else f"{filepath}.db"
            
            if os.path.exists(final_path):
                os.remove(final_path)
                
            out_conn = plain_sqlite.connect(final_path)
            out_cur = out_conn.cursor()
            
            # Generate target tables matching working structures
            out_cur.execute("CREATE TABLE families (" + ", ".join([f"{c} TEXT" if c != 'family_id' else "family_id INTEGER PRIMARY KEY" for c in family_cols]) + ")")
            out_cur.execute("CREATE TABLE people (" + ", ".join([f"{c} TEXT" if c != 'id' else "id INTEGER PRIMARY KEY" for c in people_cols]) + ")")
            
            # Inject records
            if family_rows:
                f_slots = ", ".join(["?"] * len(family_cols))
                out_cur.executemany(f"INSERT INTO families ({', '.join(family_cols)}) VALUES ({f_slots})", family_rows)
            if people_rows:
                p_slots = ", ".join(["?"] * len(people_cols))
                out_cur.executemany(f"INSERT INTO people ({', '.join(people_cols)}) VALUES ({p_slots})", people_rows)
                
            out_conn.commit()
            out_conn.close()
            return True, f"Isolated database initialized containing {len(people_rows)} matching records."
        except Exception as e:
            return False, f"SQLite3 output generation failed: {str(e)}"        
            
    def export_to_sql(self, filepath):
        """Exports the entire database as raw SQL commands using iterdump."""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                for line in self.db.conn.iterdump():
                    f.write(f"{line}\n")
            return True, "Database exported to SQL dump successfully."
        except Exception as e:
            return False, str(e)

    def import_from_sql(self, filepath):
        """Executes a raw SQL dump script to rebuild or append to the database."""
        if self.db.read_only: return False, "Database is in Read-Only mode."
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                sql_script = f.read()
            self.db.cursor.executescript(sql_script)
            self.db.conn.commit()
            return True, "SQL dump imported successfully."
        except Exception as e:
            return False, f"SQL Import Error: {str(e)}"

        
            
    def export_vcard(self, filepath, db_instance=None):
        db = db_instance if db_instance else self.db
        db.cursor.execute("PRAGMA table_info(people)")
        cols = [c[1].lower() for c in db.cursor.fetchall()]
        db.cursor.execute("SELECT * FROM people")
        rows = db.cursor.fetchall()

        count = 0
        with open(filepath, 'w', encoding='utf-8') as f:
            for row in rows:
                r = dict(zip(cols, row))
                name = str(r.get('name', '')).strip()
                if not name: continue
                
                f.write("BEGIN:VCARD\nVERSION:3.0\n")
                f.write(f"FN:{name}\n")
                f.write(f"N:;{name};;;\n")
                
                # Standard vCard Mappings
                if r.get('phone'): f.write(f"TEL;TYPE=CELL:{r.get('phone')}\n")
                if r.get('gmail'): f.write(f"EMAIL:{r.get('gmail')}\n")
                if r.get('location'): f.write(f"ADR;TYPE=HOME:;;{r.get('location')};;;;\n")
                if 'dob' in r and r.get('dob'): f.write(f"BDAY:{r.get('dob')}\n")
                if r.get('homepage'): f.write(f"URL:{r.get('homepage')}\n")
                if r.get('notes'): 
                    clean_notes = str(r.get('notes')).replace('\n', ' ').replace('\r', '')
                    f.write(f"NOTE:{clean_notes}\n")
                
                # Custom Database Mappings (Catches EVERYTHING else)
                skip_cols = ['id', 'name', 'phone', 'gmail', 'location', 'dob', 'homepage', 'notes', 'local_photo_path']
                for col in cols:
                    if col not in skip_cols and r.get(col) is not None:
                        val = str(r[col]).strip()
                        if val and val.lower() not in ['none', 'null']:
                            # Convert column 'father_name' to 'X-GEN-FATHER-NAME'
                            safe_col = col.upper().replace('_', '-')
                            safe_val = val.replace('\n', ' ').replace('\r', '')
                            f.write(f"X-GEN-{safe_col}:{safe_val}\n")

                f.write("END:VCARD\n")
                count += 1
        return count

    def import_vcard(self, filepath, db_instance=None):
        db = db_instance if db_instance else self.db
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        db.cursor.execute("PRAGMA table_info(people)")
        existing_cols = [c[1].lower() for c in db.cursor.fetchall()]

        vcards = content.split('BEGIN:VCARD')
        count = 0
        for card in vcards:
            if 'END:VCARD' not in card: continue
            lines = card.split('\n')
            
            # Start with a blank slate containing all valid columns
            data = {col: None for col in existing_cols}
            
            for line in lines:
                if not line.strip(): continue
                parts = line.split(':', 1)
                if len(parts) != 2: continue
                key, val = parts[0].strip(), parts[1].strip()
                
                # Reverse mapping from vCard back to SQLite columns
                if key == 'FN': data['name'] = val
                elif key.startswith('TEL'): data['phone'] = val
                elif key.startswith('EMAIL'): data['gmail'] = val
                elif key.startswith('ADR'): data['location'] = val.strip(';')
                elif key == 'BDAY': data['dob'] = val
                elif key.startswith('URL'): data['homepage'] = val
                elif key == 'NOTE': data['notes'] = val
                elif key.startswith('X-GEN-'):
                    # Convert 'X-GEN-FATHER-NAME' back to 'father_name'
                    col_name = key[6:].lower().replace('-', '_')
                    if col_name in existing_cols:
                        data[col_name] = val

            # Dynamically build the SQL Query based on whatever data we found
            if data.get('name'):
                insert_data = {k: v for k, v in data.items() if v is not None and k != 'id'}
                
                if insert_data:
                    columns = ", ".join(insert_data.keys())
                    placeholders = ", ".join(["?"] * len(insert_data))
                    values = tuple(insert_data.values())
                    
                    query = f"INSERT INTO people ({columns}) VALUES ({placeholders})"
                    db.cursor.execute(query, values)
                    count += 1
                
        db.conn.commit()
        return count
    
        
    def export_to_csv(self, base_path):
        """Exports the entire database structure to CSV files."""
        try:
            self.db.cursor.execute("SELECT * FROM people")
            self._write_csv(f"{base_path}_people.csv", self.db.cursor)
            self.db.cursor.execute("SELECT * FROM families")
            self._write_csv(f"{base_path}_families.csv", self.db.cursor)
            return True, ""
        except Exception as e:
            return False, str(e)

    def import_from_csv(self, people_path, families_path):
        """Imports both tables, ensuring schema compatibility."""
        if self.db.read_only:
            return False, "Database is in Read-Only mode."
        self._ensure_schema()
        try:
            dprint(f"import {families_path}")
            self._import_table(families_path, "families")
        
            with open(people_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                header = reader.fieldnames
                self.db.cursor.execute("PRAGMA table_info(people)")
                existing_cols = [row[1] for row in self.db.cursor.fetchall()]
                
                for col in header:
                    if col and col not in existing_cols:
                        self.db.cursor.execute(f"ALTER TABLE people ADD COLUMN {col} TEXT")
                self.db.conn.commit()
                
            dprint(f"import {people_path}")
            self._import_table(people_path, "people")
            return True, "Successfully imported people and families."
        except Exception as e:
            return False, f"Import error: {str(e)}"   

    def _import_table(self, file_path, table_name):
        """Imports CSV records, ensuring header-to-column alignment."""
        if self.db.read_only:
            return
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return
            
            cols = reader.fieldnames
            self.db.cursor.execute(f"PRAGMA table_info({table_name})")
            col_info = {row[1]: row[2].upper() for row in self.db.cursor.fetchall()}
            
            placeholders = ", ".join(["?"] * len(cols))
            sql = f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols)}) VALUES ({placeholders})"
            
            for row in reader:
                vals = []
                for col in cols:
                    val = row.get(col)
                    val_str = str(val) if val is not None else ""
                    
                    if col in col_info and "INT" in col_info[col]:
                        vals.append(int(val_str) if val_str.strip() else None)
                    else:
                        vals.append(val if val and str(val).strip() else None)
                
                self.db.cursor.execute(sql, vals)
                
        self.db.conn.commit()
                    
    def get_file_info(self, filepath):
        if not os.path.exists(filepath): return None
        size = os.path.getsize(filepath)
        sha256_hash = hashlib.sha256()
        with open(filepath, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return {"size_bytes": size, "checksum": sha256_hash.hexdigest()}

    def export_to_json(self, filename="genealogy_data.json"):
        try:
            current_time_ist = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
            db_path = getattr(self.db, 'db_path', "unknown.db")
            
            data = {
                "metadata": {
                    "db_name": db_path,
                    "export_time_ist": current_time_ist, 
                    "version": __rcs_id__,
                    "file_status": {
                        "program": self.get_file_info("genealogy.py"),
                        "database": self.get_file_info(db_path)
                    }
                },
                "families": [], 
                "people": []
            }
            for table in ["families", "people"]:
                self.db.cursor.execute(f"PRAGMA table_info({table})")
                columns = [info[1] for info in self.db.cursor.fetchall()]
                
                self.db.cursor.execute(f"SELECT * FROM {table}")
                for row in self.db.cursor.fetchall():
                    data[table].append(dict(zip(columns, row)))
                    
            with open(filename, 'w') as f:
                json.dump(data, f, indent=4)
            return True, f"Exported records to JSON."
        except Exception as e:
            return False, str(e)
                
    def import_from_json(self, filepath):
        """Imports family and people data from a standard JSON dictionary dump."""
        try:
            import json
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            people_count = 0
            family_count = 0
            
            # 1. Import Families
            if 'families' in data:
                for fam in data['families']:
                    # Use your existing database insertion mechanisms
                    self.db.cursor.execute(
                        "INSERT INTO families (family_id, family_group, family_name) VALUES (?, ?, ?)",
                        (fam.get('family_id'), fam.get('family_group'), fam.get('family_name'))
                    )
                    family_count += 1
                    
            # 2. Import People
            if 'people' in data:
                for person in data['people']:
                    self.db.cursor.execute(
                        "INSERT INTO people (id, name, family_group, family_name, family_id) VALUES (?, ?, ?, ?, ?)",
                        (person.get('id'), person.get('name'), person.get('family_group'), person.get('family_name'), person.get('family_id'))
                    )
                    people_count += 1
                    
            self.db.conn.commit()
            
            # === FIXED FOR v20.01: Explicitly return the success tuple ===
            return True, f"Successfully imported {people_count} people and {family_count} families."
            
        except Exception as e:
            self.db.conn.rollback() # Safely undo partial imports if it crashes
            # === FIXED FOR v20.01: Explicitly return the failure tuple ===
            return False, f"JSON Import Error: {str(e)}"

    def import_from_gedcom(self, file_path):
        """Robust GEDCOM parser handling UTF-16 and UTF-8 encodings."""
        if self.db.read_only:
            return False, "Database is in Read-Only mode."
        self._ensure_schema()
        lines = []
        try:
            with open(file_path, 'r', encoding='utf-16') as f:
                lines = f.readlines()
        except (UnicodeDecodeError, UnicodeError):
            try:
                with open(file_path, 'r', encoding='utf-8-sig') as f:
                    lines = f.readlines()
            except Exception as e:
                return False, f"Encoding error: {e}"

        try:
            count = 0
            current_person = {'name': '', 'notes': '', 'family_group': ''}
            
            for line in lines:
                parts = line.strip().split(' ', 2)
                if len(parts) < 2: continue
                
                level, tag = parts[0], parts[1]
                value = parts[2] if len(parts) > 2 else ""

                if level == '0' and 'INDI' in value:
                    if current_person.get('name'):
                        self._save_gedcom_person(current_person)
                        count += 1
                    current_person = {'name': '', 'notes': '', 'family_group': ''}
                
                elif tag == 'NAME':
                    current_person['name'] = value.replace('/', '').strip()
                    if '/' in value:
                        current_person['family_group'] = value.split('/')[1].strip()
                elif tag == 'NOTE':
                    current_person['notes'] += value + " "

            if current_person.get('name'):
                self._save_gedcom_person(current_person)
                count += 1
                
            self.db.conn.commit()
            return True, f"Imported {count} individuals."
        except Exception as e:
            return False, str(e)

    def _save_gedcom_person(self, p_dict):
        if self.db.read_only: return
        sql = "INSERT OR IGNORE INTO people (name, family_group, notes) VALUES (?, ?, ?)"
        self.db.cursor.execute(sql, (p_dict['name'], p_dict['family_group'], p_dict['notes'].strip()))
           
    def export_to_gedcom(self, file_path):
        """Exports individuals and their family links to GEDCOM."""
        try:
            self.db.cursor.execute("SELECT * FROM people")
            rows = self.db.cursor.fetchall()
            colnames = [d[0] for d in self.db.cursor.description]

            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("0 HEAD\n1 GEDC\n2 VERS 5.5.1\n2 FORM LINEAGE-LINKED\n1 CHAR UTF-8\n")
                families = {} 

                for row in rows:
                    data = dict(zip(colnames, row))
                    p_id = data['id']
                    f_id = data.get('father_id')
                    m_id = data.get('mother_id')

                    f.write(f"0 @I{p_id}@ INDI\n")
                    if data.get('name'): f.write(f"1 NAME {data['name']}\n")
                    if data.get('family_group'): f.write(f"2 SURN {data['family_group']}\n")
                    if data.get('notes'): f.write(f"1 NOTE {data['notes']}\n")

                    if f_id or m_id:
                        fam_key = (f_id, m_id)
                        if fam_key not in families: families[fam_key] = []
                        families[fam_key].append(p_id)
                        f.write(f"1 FAMC @F{hash(fam_key) % 10000}@\n")

                for (f_id, m_id), children in families.items():
                    fam_id = hash((f_id, m_id)) % 10000
                    f.write(f"0 @F{fam_id}@ FAM\n")
                    if f_id: f.write(f"1 HUSB @I{f_id}@\n")
                    if m_id: f.write(f"1 WIFE @I{m_id}@\n")
                    for c_id in children:
                        f.write(f"1 CHIL @I{c_id}@\n")
                
                f.write("0 TRLR\n")
                
            return True, f"Exported {len(rows)} records and {len(families)} family links."
        except Exception as e:
            return False, str(e)
        
    def _ensure_schema(self):
        dprint("_ansure_schema")
        self.db.create_base_table()
        self.db.sync_schema()

    def export_to_unencrypted_sqlite(self, target_path):
        """Exports the encrypted database to a plain, unencrypted SQLite3 file."""
        try:
            import sqlite3 as plain_sqlite
            target_conn = plain_sqlite.connect(target_path)
            self.db.cursor.execute(f"ATTACH DATABASE '{target_path}' AS plaintext KEY ''")
            self.db.cursor.execute("SELECT sqlcipher_export('plaintext')")
            self.db.cursor.execute("DETACH DATABASE plaintext")
            target_conn.close()
            return True, f"Successfully exported to unencrypted database at: {target_path}"
        except Exception as e:
            return False, f"Export failed: {str(e)}"

    def get_kinship_term(self, ups, downs, marriages, path_sequence, target_sex=None):
        """Translates graph traversal steps into natural English relationship terms based on sex."""
        sex_str = str(target_sex).strip().upper() if target_sex else ""
        is_m = sex_str in ['M', 'MALE']
        is_f = sex_str in ['F', 'FEMALE']

        standard = True
        seen_down = False
        for step in path_sequence:
            if step == 'DOWN': seen_down = True
            elif step == 'UP' and seen_down: standard = False; break
                
        if not standard and marriages == 0: return "Distant/Complex Blood Relative"
        if marriages > 1: return "Extended In-Law / Complex Connection"

        base_term = ""
        if ups == 0 and downs == 0:
            if marriages == 1: return "Husband" if is_m else "Wife" if is_f else "Spouse"
            return "Self"
        elif ups == 1 and downs == 0: base_term = "Father" if is_m else "Mother" if is_f else "Parent"
        elif ups == 2 and downs == 0: base_term = "Grandfather" if is_m else "Grandmother" if is_f else "Grandparent"
        elif ups > 2 and downs == 0: 
            prefix = 'Great-' * (ups - 2)
            base_term = f"{prefix}Grandfather" if is_m else f"{prefix}Grandmother" if is_f else f"{prefix}Grandparent"
        elif ups == 0 and downs == 1: base_term = "Son" if is_m else "Daughter" if is_f else "Child"
        elif ups == 0 and downs == 2: base_term = "Grandson" if is_m else "Granddaughter" if is_f else "Grandchild"
        elif ups == 0 and downs > 2: 
            prefix = 'Great-' * (downs - 2)
            base_term = f"{prefix}Grandson" if is_m else f"{prefix}Granddaughter" if is_f else f"{prefix}Grandchild"
        elif ups == 1 and downs == 1: base_term = "Brother" if is_m else "Sister" if is_f else "Sibling"
        elif ups >= 2 and downs == 1:
            if ups == 2: base_term = "Uncle" if is_m else "Aunt" if is_f else "Aunt / Uncle"
            else: 
                prefix = 'Great-' * (ups - 3)
                base_term = f"{prefix}Great-Uncle" if is_m else f"{prefix}Great-Aunt" if is_f else f"{prefix}Great-Aunt / Uncle"
        elif ups == 1 and downs >= 2:
            if downs == 2: base_term = "Nephew" if is_m else "Niece" if is_f else "Niece / Nephew"
            else: 
                prefix = 'Great-' * (downs - 3)
                base_term = f"{prefix}Great-Nephew" if is_m else f"{prefix}Great-Niece" if is_f else f"{prefix}Great-Niece / Nephew"
        elif ups >= 2 and downs >= 2:
            cousin_degree = min(ups, downs) - 1
            removed = abs(ups - downs)
            ordinal = lambda n: "%d%s" % (n, "tsnrhtdd"[(n//10%10!=1)*(n%10<4)*n%10::4]) 
            base_term = f"{ordinal(cousin_degree)} Cousin"
            if removed > 0: base_term += f" {removed}x removed"

        if marriages == 1:
            if path_sequence[0] == 'MAR' and base_term:
                if "Cousin" not in base_term: return f"{base_term}-in-law"
                return f"Spouse's {base_term}"
            elif path_sequence[-1] == 'MAR' and base_term:
                if "Cousin" not in base_term: return f"{base_term}-in-law"
                return f"{base_term}'s Spouse"
            else:
                return "Step-Relative / Complex In-Law"
                
        return base_term

    def find_relationship(self, person1_input, person2_input):
        import networkx as nx

        def resolve_id(user_input):
            input_str = str(user_input).strip()
            if not input_str: return None
            if input_str.isdigit(): return int(input_str)
            if "(" in input_str and ")" in input_str:
                try:
                    possible_id = input_str.split("(")[-1].replace(")", "").strip()
                    if possible_id.isdigit(): return int(possible_id)
                except: pass
            self.db.cursor.execute("SELECT id FROM people WHERE name = ?", (input_str,))
            res = self.db.cursor.fetchone()
            return res[0] if res else None

        id1 = resolve_id(person1_input)
        id2 = resolve_id(person2_input)

        if not id1 or not id2: return False, "Could not resolve one or both inputs to a valid database record.", "", [], []
        if id1 == id2: return True, "Calculated Relationship: Self\nThey are the exact same person.", "", [], []

        self.db.cursor.execute("PRAGMA table_info(people)")
        cols = [r[1].lower() for r in self.db.cursor.fetchall()]
        if 'sex' in cols:
            self.db.cursor.execute("SELECT id, name, father_id, mother_id, husband_ids, wife_ids, sex FROM people")
        else:
            self.db.cursor.execute("SELECT id, name, father_id, mother_id, husband_ids, wife_ids, '' as sex FROM people")
            
        rows = self.db.cursor.fetchall()
        G = nx.Graph()
        name_map = {}
        sex_map = {}

        for row in rows:
            p_id, name, f_id, m_id, h_ids, w_ids, p_sex = row
            G.add_node(p_id)
            name_map[p_id] = name
            sex_map[p_id] = p_sex

            if f_id: G.add_edge(p_id, f_id, rel='Child-Parent')
            if m_id: G.add_edge(p_id, m_id, rel='Child-Parent')

            for sp_str in [h_ids, w_ids]:
                if sp_str:
                    spouses = [s.strip() for s in str(sp_str).split(',')]
                    for sp in spouses:
                        extracted_id = None
                        if "(" in sp and ")" in sp:
                            try: extracted_id = sp.split("(")[-1].split(")")[0].strip()
                            except: pass
                        elif sp.isdigit(): extracted_id = sp

                        if extracted_id and extracted_id.isdigit():
                            G.add_edge(p_id, int(extracted_id), rel='Spouse')

        try: path = nx.shortest_path(G, source=id1, target=id2)
        except nx.NetworkXNoPath:
            msg = f"No relationship path found between {name_map.get(id1, id1)} and {name_map.get(id2, id2)}."
            return True, msg, msg, [], []
        except nx.NodeNotFound: return False, "One of the IDs does not exist in the graph structure.", "", [], []

        steps = []
        ups, downs, marriages = 0, 0, 0
        path_sequence = []
        edge_list = []

        for i in range(len(path) - 1):
            curr_node = path[i]
            next_node = path[i+1]
            edge_data = G.get_edge_data(curr_node, next_node)
            rel_type = edge_data.get('rel', 'Connected')

            if rel_type == 'Child-Parent':
                self.db.cursor.execute("SELECT father_id, mother_id FROM people WHERE id = ?", (curr_node,))
                parents = self.db.cursor.fetchone()
                if parents and next_node in parents:
                    rel_desc = "is the child of"
                    ups += 1
                    path_sequence.append('UP')
                    edge_list.append('child of')
                else:
                    rel_desc = "is the parent of"
                    downs += 1
                    path_sequence.append('DOWN')
                    edge_list.append('parent of')
            elif rel_type == 'Spouse':
                rel_desc = "is the spouse of"
                marriages += 1
                path_sequence.append('MAR')
                edge_list.append('spouse of')
            else:
                rel_desc = "is related to"
                edge_list.append('related to')

            steps.append(f"{name_map[curr_node]}  --({rel_desc})-->  {name_map[next_node]}")

        target_person_sex = sex_map.get(id2)
        kinship_term = self.get_kinship_term(ups, downs, marriages, path_sequence, target_sex=target_person_sex)
        
        report_kinship = f"Calculated Relationship: {kinship_term}"
        report_standard = f"Path ({len(path)-1} degrees of separation):\n\n" + "\n".join(steps)
        path_names = [name_map[n] for n in path]
            
        return True, report_kinship, report_standard, path_names, edge_list

    def find_related_people(self, base_id, relation_key):
        """Returns a list of person rows matching a relation key for a specific base ID."""
        relation_key = relation_key.strip().lower()
        
        # 1. Fetch Children (Leveraging your existing get_children query logic)
        if relation_key in ['children', 'child', 'son', 'daughter']:
            self.db.cursor.execute(
                "SELECT id, name, family_name FROM people WHERE father_id = ? OR mother_id = ?", 
                (base_id, base_id)
            )
            return self.db.cursor.fetchall()
            
        # 2. Fetch Parents
        elif relation_key in ['parents', 'parent', 'father', 'mother']:
            self.db.cursor.execute("SELECT father_id, mother_id FROM people WHERE id = ?", (base_id,))
            res = self.db.cursor.fetchone()
            if not res: 
                return []
                
            parent_ids = [pid for pid in res if pid is not None]
            if not parent_ids: 
                return []
                
            placeholders = ", ".join(["?"] * len(parent_ids))
            self.db.cursor.execute(f"SELECT id, name, family_name FROM people WHERE id IN ({placeholders})", parent_ids)
            return self.db.cursor.fetchall()
            
        # 3. Fetch Spouses (Parsing the comma-separated husband/wife string fields)
        elif relation_key in ['spouses', 'spouse', 'husband', 'wife']:
            self.db.cursor.execute("SELECT husband_ids, wife_ids FROM people WHERE id = ?", (base_id,))
            res = self.db.cursor.fetchone()
            if not res: 
                return []
                
            # Collect and merge IDs from both spouse tracking strings
            raw_ids = []
            for id_string in res:
                if id_string:
                    raw_ids.extend([s.strip() for s in str(id_string).split(',') if s.strip().isdigit()])
            
            if not raw_ids: 
                return []
                
            placeholders = ", ".join(["?"] * len(raw_ids))
            self.db.cursor.execute(f"SELECT id, name, family_name FROM people WHERE id IN ({placeholders})", [int(x) for x in raw_ids])
            return self.db.cursor.fetchall()
            
        else:
            print(f"[-] Unknown relationship key: '{relation_key}'")
            return []

    def find_extended_relatives(self, base_id, target_relationship_label):
        """Scans the entire NetworkX family graph to find everyone who matches 

        an exact relationship type string (e.g., '1st Cousin', 'Uncle', 'Nephew').
        """
        target_label = target_relationship_label.strip().lower()
        matched_relatives = []
        
        # Pull all active records to check valid IDs
        self.db.cursor.execute("SELECT id, name FROM people")
        all_people = self.db.cursor.fetchall()
        
        print(f"[*] Analyzing family network paths relative to ID {base_id}...")
        
        for candidate_id, candidate_name in all_people:
            if candidate_id == base_id:
                continue
                
            # Utilize your internal path calculation engine pass
            success, kinship_rep, _, _, _ = self.find_relationship(base_id, candidate_id)
            
            if success and kinship_rep:
                # Extracts the raw kinship term from "Calculated Relationship: X"
                current_term = kinship_rep.replace("Calculated Relationship:", "").strip().lower()
                
                # Check if the generated kinship string matches your lookup token
                if target_label in current_term:
                    matched_relatives.append((candidate_id, candidate_name, current_term))
                    
        return matched_relatives


class GenealogyData:
    def __init__(self, engine_type="sqlite3", db_name="family.db", password=None, read_only=False):
        self.read_only = read_only
        if engine_type == "sqlite3":
            import sqlite3
        else:
            import sqlcipher3 as sqlite3
            
        dprint(f"opening {db_name}")
        self.conn = sqlite3.connect(db_name)
        self.db_path = db_name
        self.cursor = self.conn.cursor()

        if password:
            self.cursor.execute(f"PRAGMA key = '{password}'")
        
        self.create_base_table() 
        self.sync_schema()

    def create_base_table(self):
        dprint("create_base_table :")    
        if self.read_only: return
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_metadata (
        key TEXT PRIMARY KEY,
        value TEXT
        )        
        """)
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY, 
        name TEXT NOT NULL,
        family_id INTEGER,
        family_group TEXT,
        family_name TEXT,            
        ancestral_family_name TEXT,  
        UNIQUE(name, family_name)
        )
        """)
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS families (
        family_id INTEGER PRIMARY KEY,
        family_group TEXT,
        family_name TEXT UNIQUE,
        family_notes TEXT      
        )
        """)
        self.conn.commit()

    def clear_all_data(self):
        if self.read_only: return
        dprint("clear_all_data")
        self.cursor.execute("DELETE FROM people")
        self.cursor.execute("DELETE FROM families")
        self.cursor.execute("DELETE FROM sqlite_sequence WHERE name='people'")
        self.conn.commit()

    def sync_schema(self):
        dprint("sync_schema")
        if self.read_only: return
        schema_def = {
            "people": {
                "id": "INTEGER PRIMARY KEY",
                "name": "TEXT NOT NULL",
                "family_id": "INTEGER",
                "family_group": "TEXT",
                "family_name": "TEXT",
                "ancestral_family_name": "TEXT", 
                "father_id": "INTEGER", 
                "mother_id": "INTEGER", 
                "husband_ids": "TEXT", 
                "wife_ids": "TEXT", 
                "son_ids": "TEXT", 
                "daughter_ids": "TEXT", 
                "surname": "TEXT",
                "dob": "TEXT",
                "nicknames": "TEXT", 
                "other_names": "TEXT",
                "sex": "TEXT",
                "religion": "TEXT",
                "caste": "TEXT",               
                "location": "TEXT", 
                "office_address": "TEXT", 
                "office_contact": "TEXT", 
                "phone": "TEXT", 
                "other_phones": "TEXT", 
                "whatsapp": "TEXT", 
                "telegram": "TEXT", 
                "gmail": "TEXT", 
                "google_photos": "TEXT", 
                "facebook": "TEXT", 
                "homepage": "TEXT", 
                "local_photo_path": "TEXT", 
                "notes": "TEXT",
                "conf_notes": "TEXT",
                "instagram": "TEXT", 
                "diceased": "BOOL", 
                "deceased": "INTEGER DEFAULT 0"
            },
            "families": {
                "family_id": "TEXT",
                "family_group": "TEXT",     
                "family_name": "TEXT",      
                "ancestral_family_name": "TEXT",
                "family_notes": "TEXT"
            }
        }

        for table, columns in schema_def.items():
            self.cursor.execute(f"PRAGMA table_info({table})")
            existing = [row[1] for row in self.cursor.fetchall()]
            
            for col, col_type in columns.items():
                if col not in existing:
                    dprint(f"adding col {col}")
                    self.cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                    
        self.conn.commit()

    def get_children(self, person_id):
        dprint("get_children")
        self.cursor.execute("SELECT id, name FROM people WHERE father_id = ? OR mother_id = ?", (person_id, person_id))
        return self.cursor.fetchall()

    def remove_duplicates(self):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("remove dups")
        try:
            self.cursor.execute("""
            DELETE FROM people 
            WHERE id NOT IN (
            SELECT MIN(id) 
            FROM people 
            GROUP BY name, family_group
            )
            """)
            changes = self.conn.total_changes
            self.cursor.commit()
            return True, f"Removed {changes} duplicate records."
        except Exception as e:
            return False, str(e)

    def delete_person(self, person_id):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("delete_person")
        try:
            self.cursor.execute("UPDATE people SET father_id = NULL WHERE father_id = ?", (person_id,))
            self.cursor.execute("UPDATE people SET mother_id = NULL WHERE mother_id = ?", (person_id,))
            self.cursor.execute("DELETE FROM people WHERE id = ?", (person_id,))
            self.conn.commit()
            return True, "Person deleted successfully. Children unlinked."
        except Exception as e:
            return False, str(e)

    def delete_branch_recursive(self, person_id):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("delete_branch_recursive")
        try:
            children = self.get_children(person_id)
            for c_id, _ in children:
                self.delete_branch_recursive(c_id)
                
            self.cursor.execute("DELETE FROM people WHERE id = ?", (person_id,))
            return True
        except Exception as e:
            return False, str(e)

    def add_parent(self, person_id, parent_type="father"):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("add_parent")
        try:
            self.cursor.execute("INSERT INTO people (name) VALUES (?)", (f"New {parent_type.capitalize()}",))
            parent_id = self.cursor.lastrowid
            self.cursor.execute(f"UPDATE people SET {parent_type}_id = ? WHERE id = ?", (parent_id, person_id))
            self.conn.commit()
            return True, parent_id
        except Exception as e:
            return False, str(e)

    def unlink_parents(self, person_id):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("unlink parent")
        try:
            self.cursor.execute("UPDATE people SET father_id = NULL, mother_id = NULL WHERE id = ?", (person_id,))
            self.conn.commit()
            return True, "Parents unlinked successfully."
        except Exception as e:
            return False, str(e)

    def delete_person_and_unlink_children(self, person_id):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("delete_person_and_unlink_children")
        try:
            self.cursor.execute("UPDATE people SET father_id = NULL WHERE father_id = ?", (person_id,))
            self.cursor.execute("UPDATE people SET mother_id = NULL WHERE mother_id = ?", (person_id,))
            self.cursor.execute("DELETE FROM people WHERE id = ?", (person_id,))
            self.conn.commit()
            return True, "Person deleted. Children unlinked."
        except Exception as e:
            return False, str(e)

    def delete_children_only(self, person_id):
        if self.read_only: return False, "Database is in Read-Only mode."
        dprint("delete_children_only")
        try:
            self.cursor.execute("SELECT id FROM people WHERE father_id = ? OR mother_id = ?", (person_id, person_id))
            children_ids = [row[0] for row in self.cursor.fetchall()]
            
            if not children_ids:
                return True, "No children found to delete."
                
            for c_id in children_ids:
                self.cursor.execute("DELETE FROM people WHERE id = ?", (c_id,))
                
            self.conn.commit()
            return True, f"Successfully deleted {len(children_ids)} children."
        except Exception as e:
            return False, str(e)
        

class AddFieldDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Add Custom Field", size=(300, 220))
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        self.name_ctrl = wx.TextCtrl(self)
        self.multiline_cb = wx.CheckBox(self, label="Multiline (for addresses/notes)")
        
        vbox.Add(wx.StaticText(self, label="Field Display Name:"), 0, wx.ALL, 10)
        vbox.Add(self.name_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        vbox.Add(self.multiline_cb, 0, wx.ALL, 10)
        
        btn_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
        
        self.SetSizer(vbox)

class PasswordDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Security Check", size=(350, 200))
        main_vbox = wx.BoxSizer(wx.VERTICAL)
        
        lbl = wx.StaticText(self, label="Enter Database Password:")
        self.password_ctrl = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self.save_cb = wx.CheckBox(self, label="Save password in config.json")
        btn_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        
        main_vbox.Add(lbl, 0, wx.ALL, 10)
        main_vbox.Add(self.password_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        main_vbox.Add(self.save_cb, 0, wx.ALL, 10)
        main_vbox.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
        
        self.SetSizer(main_vbox)
        self.Center() 
        
    def GetValue(self):
        return self.password_ctrl.GetValue()
    
    def ShouldSave(self):
        return self.save_cb.IsChecked()

class EditPeopleDialog(wx.Dialog):
    def __init__(self, parent, db):
        super().__init__(parent, title="Edit People Table", size=(1000, 800), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.db = db
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        self.search_ctrl = wx.SearchCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.SetHint("Type to filter...")
        self.search_ctrl.Bind(wx.EVT_TEXT, self.on_search)
        vbox.Add(self.search_ctrl, 0, wx.EXPAND | wx.ALL, 5)
        
        self.db.cursor.execute("PRAGMA table_info(people)")
        self.cols = [row[1] for row in self.db.cursor.fetchall()]
        
        self.grid = gridlib.Grid(panel)
        self.grid.CreateGrid(0, len(self.cols))
        self.grid.Bind(wx.grid.EVT_GRID_COL_SORT, self.on_sort_column)
        self.grid.EnableDragColSize(True)

        self.grid.EnableDragColMove(True)
        
        if self.db.read_only:
            self.grid.EnableEditing(False)
        
        for i, col in enumerate(self.cols):            
            self.grid.SetColLabelValue(i, col)

        self.load_data()

        if not self.db.read_only:
            for i, col in enumerate(self.cols):
                attr = wx.grid.GridCellAttr()
                self.db.cursor.execute(f"SELECT distinct {self.cols[i]} FROM people order by {self.cols[i]}")            
                val = self.db.cursor.fetchall()
                comp = []
                [comp.append(d[0]) if d[0] else False for d in val]
                if comp and comp[0]:
                    attr.SetEditor(GridCellAutoCompleteEditor(comp))
                    self.grid.SetColAttr(i, attr)
                    
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="Add Person")
        self.btn_dup = wx.Button(panel, label="Duplicate Person")
        self.btn_del = wx.Button(panel, label="Delete Selected")
        self.btn_save = wx.Button(panel, label="Save All Changes")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_dup.Bind(wx.EVT_BUTTON, self.on_duplicate)
        self.btn_del.Bind(wx.EVT_BUTTON, self.on_delete)
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_dup, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_del, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_save, 0, wx.ALL, 5)
        
        if self.db.read_only:
            self.btn_add.Disable()
            self.btn_dup.Disable()
            self.btn_del.Disable()
            self.btn_save.Disable()
        
        vbox.Add(self.grid, 1, wx.EXPAND | wx.ALL, 5)
        vbox.Add(btn_sizer, 0, wx.ALIGN_CENTER)
        panel.SetSizer(vbox)

    def on_duplicate(self, event):
        if self.db.read_only: return
        
        row = self.grid.GetGridCursorRow()
        if row < 0:
            wx.MessageBox("Please click on a row to duplicate.", "No Selection", wx.ICON_INFORMATION)
            return
            
        # Append a new row at the bottom
        self.grid.AppendRows(1)
        new_row = self.grid.GetNumberRows() - 1
        
        # Copy data column by column
        for col in range(len(self.cols)):
            if self.cols[col].lower() == 'id':
                # Leave ID blank so it generates a fresh one upon saving
                self.grid.SetCellValue(new_row, col, "")
            elif self.cols[col].lower() == 'name':
                # Append '(Copy)' to avoid SQLite UNIQUE constraint crashes
                old_name = self.grid.GetCellValue(row, col)
                self.grid.SetCellValue(new_row, col, f"{old_name} (Copy)")
            else:
                # Inherit all other details directly
                self.grid.SetCellValue(new_row, col, self.grid.GetCellValue(row, col))
                
        # Scroll the grid down so the user immediately sees the duplicated row
        self.grid.MakeCellVisible(new_row, 0)

        
    def on_add(self, event):
        if self.db.read_only: return
        self.grid.AppendRows(1)

    def on_delete(self, event):
        if self.db.read_only: return
        row = self.grid.GetGridCursorRow()
        if row < 0: return
        p_id = self.grid.GetCellValue(row, 0)
        
        if p_id and str(p_id).strip():
            if wx.MessageBox("Are you sure you want to permanently delete this person?", "Confirm Delete", wx.YES_NO | wx.ICON_WARNING) == wx.YES:
                self.db.cursor.execute("DELETE FROM people WHERE id = ?", (p_id,))
                # Unlink from children to prevent database orphans
                self.db.cursor.execute("UPDATE people SET father_id = NULL WHERE father_id = ?", (p_id,))
                self.db.cursor.execute("UPDATE people SET mother_id = NULL WHERE mother_id = ?", (p_id,))
                self.db.conn.commit()
                self.grid.DeleteRows(row, 1)
        else:
            # If the row hasn't been saved to the DB yet, just remove it from the grid
            self.grid.DeleteRows(row, 1)

    def on_save(self, event):
        if self.db.read_only: return
        for i in range(self.grid.GetNumberRows()):
            # v19.57 FIX: Convert empty grid strings back to Python None (SQLite NULL)
            row_data = []
            for j in range(len(self.cols)):
                val = self.grid.GetCellValue(i, j).strip()
                row_data.append(val if val != "" else None)
                
            p_id = row_data[0]
            p_name = row_data[1] if len(row_data) > 1 and row_data[1] else "Unknown" 
            
            try:
                if p_id:
                    # Update existing record
                    updates = ", ".join([f"{self.cols[j]} = ?" for j in range(1, len(self.cols))])
                    params = row_data[1:] + [p_id]
                    self.db.cursor.execute(f"UPDATE people SET {updates} WHERE id = ?", params)
                else:
                    # Insert new record
                    cols_to_insert = self.cols[1:]
                    vals_to_insert = row_data[1:]
                    
                    name_idx = cols_to_insert.index('name') if 'name' in cols_to_insert else 0
                    if not vals_to_insert[name_idx]:
                        vals_to_insert[name_idx] = "New Person"
                        
                    placeholders = ", ".join(["?"] * len(cols_to_insert))
                    col_names = ", ".join(cols_to_insert)
                    self.db.cursor.execute(f"INSERT INTO people ({col_names}) VALUES ({placeholders})", vals_to_insert)
                    
                    # Update the grid with the new ID so subsequent saves don't duplicate it
                    new_id = self.db.cursor.lastrowid
                    self.grid.SetCellValue(i, 0, str(new_id))
                    
            except Exception as e:
                # Capture the specific error and row details
                error_msg = f"Failed to save Row {i + 1}:\n\n"
                error_msg += f"Person ID: {p_id if p_id else 'NEW'}\n"
                error_msg += f"Name: {p_name}\n\n"
                error_msg += f"Database Error: {str(e)}\n\n"
                error_msg += "Save aborted. Please fix the conflicting data and try again."
                
                wx.MessageBox(error_msg, "Save Error", wx.ICON_ERROR)
                self.db.conn.rollback() # Cancel any partial saves
                return
        
        self.db.conn.commit()
        wx.MessageBox("People records saved successfully.", "Success")
        
    def on_sort_column(self, event):
        col = event.GetCol()
        data = []
        for i in range(self.grid.GetNumberRows()):
            row = [self.grid.GetCellValue(i, j) for j in range(self.grid.GetNumberCols())]
            data.append(row)
        
        def sort_key(row):
            val = row[col].strip()
            try:
                return float(val)
            except ValueError:
                return val.lower()

        data.sort(key=sort_key)
        
        if hasattr(self, '_last_col') and self._last_col == col and not getattr(self, '_desc', False):
            data.reverse()
            self._desc = True
        else:
            self._desc = False
        
        self._last_col = col
        
        self.grid.BeginBatch()
        for i, row in enumerate(data):
            for j, val in enumerate(row):
                self.grid.SetCellValue(i, j, val)
        self.grid.EndBatch()
        self.grid.ForceRefresh()

    def load_data(self):
        self.db.cursor.execute("SELECT * FROM people")
        self.original_data = self.db.cursor.fetchall()
        self.update_grid(self.original_data)

    def update_grid(self, data):
        self.grid.ClearGrid()
        if self.grid.GetNumberRows() > 0:
            self.grid.DeleteRows(0, self.grid.GetNumberRows())
        
        self.grid.AppendRows(len(data))
        for i, row in enumerate(data):
            for j, val in enumerate(row):
                self.grid.SetCellValue(i, j, str(val) if val is not None else "")

    def on_search(self, event):
        query = self.search_ctrl.GetValue().lower()
        filtered_data = [
            row for row in self.original_data 
            if any(query in str(cell).lower() for cell in row if cell is not None)
        ]        
        self.update_grid(filtered_data)
    
class EditFamiliesDialog(wx.Dialog):
    def __init__(self, parent, db):
        super().__init__(parent, title="Edit Families Table", size=(1000, 800), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.db = db

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        self.search_ctrl = wx.SearchCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.SetHint("Type to filter...")
        self.search_ctrl.Bind(wx.EVT_TEXT, self.on_search)
        vbox.Add(self.search_ctrl, 0, wx.EXPAND | wx.ALL, 5)
        
        self.grid = gridlib.Grid(panel)
        self.grid.CreateGrid(0, 5) 
        self.grid.SetDefaultColSize(150, True)

        self.grid.EnableDragColMove(True)
        
        self.grid.SetColLabelValue(0, "ID")
        self.grid.SetColLabelValue(1, "family_group")
        self.grid.SetColLabelValue(2, "family_name")
        self.grid.SetColLabelValue(3, "ancestor_family_name")
        self.grid.SetColLabelValue(4, "Notes")

        self.grid.Bind(wx.grid.EVT_GRID_COL_SORT, self.on_sort_column)
        
        if self.db.read_only:
            self.grid.EnableEditing(False)
            
        self.load_data()
        
        self.db.cursor.execute("PRAGMA table_info(families)")
        self.cols = [row[1] for row in self.db.cursor.fetchall()]

        if not self.db.read_only:
            for i, col in enumerate(self.cols):
                attr = wx.grid.GridCellAttr()
                self.db.cursor.execute(f"SELECT distinct {self.cols[i]} FROM families order by {self.cols[i]}")
                val = self.db.cursor.fetchall()
                comp = []
                [comp.append(d[0]) if d[0] else False for d in val]
                if comp and comp[0]:
                    attr.SetEditor(GridCellAutoCompleteEditor(comp))
                    self.grid.SetColAttr(i, attr)
            
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="Add New")
        self.btn_dup = wx.Button(panel, label="Duplicate Family")
        self.btn_del = wx.Button(panel, label="Delete Selected")
        self.btn_save = wx.Button(panel, label="Save Changes")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_dup.Bind(wx.EVT_BUTTON, self.on_duplicate)
        self.btn_del.Bind(wx.EVT_BUTTON, self.on_delete)
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save)
        self.Bind(wx.EVT_SIZE, self.on_resize)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_dup, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_del, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_save, 0, wx.ALL, 5)
        
        if self.db.read_only:
            self.btn_add.Disable()
            self.btn_dup.Disable()
            self.btn_del.Disable()
            self.btn_save.Disable()
        
        vbox.Add(self.grid, 1, wx.EXPAND | wx.ALL, 10)
        vbox.Add(btn_sizer, 0, wx.ALIGN_CENTER)
        panel.SetSizer(vbox)
        main_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(main_sizer)

    def on_duplicate(self, event):
        if self.db.read_only: return
        
        row = self.grid.GetGridCursorRow()
        if row < 0:
            wx.MessageBox("Please click on a row to duplicate.", "No Selection", wx.ICON_INFORMATION)
            return
            
        # Append a new row at the bottom
        self.grid.AppendRows(1)
        new_row = self.grid.GetNumberRows() - 1
        
        # Columns: 0: ID, 1: family_group, 2: family_name, 3: ancestor_family_name, 4: Notes
        for col in range(5):
            if col == 0:
                # Leave ID blank so it generates a fresh one upon saving
                self.grid.SetCellValue(new_row, col, "")
            elif col == 2:
                # Append '(Copy)' to avoid SQLite UNIQUE constraint crashes
                old_name = self.grid.GetCellValue(row, col)
                self.grid.SetCellValue(new_row, col, f"{old_name} (Copy)")
            else:
                # Inherit all other details directly
                self.grid.SetCellValue(new_row, col, self.grid.GetCellValue(row, col))
                
        # Scroll the grid down so the user immediately sees the duplicated row
        self.grid.MakeCellVisible(new_row, 0)
        
    def update_grid_columns(self, event=None):
        total_width = self.grid.GetClientSize().width
        fixed_id_width = 25
        
        max_name_w = 100 
        max_nick_w = 100 
        max_anc_w = 100 
        
        for i in range(self.grid.GetNumberRows()):
            name_val = self.grid.GetCellValue(i, 1)
            nick_val = self.grid.GetCellValue(i, 2)
            anc_val = self.grid.GetCellValue(i, 3)
            
            max_name_w = max(max_name_w, len(name_val) * 9 + 20)
            max_nick_w = max(max_nick_w, len(nick_val) * 9 + 20)
            max_anc_w = max(max_anc_w, len(anc_val) * 9 + 20)
            
        name_col_w = min(max_name_w, 200)
        nick_col_w = min(max_nick_w, 200)
        anc_col_w = min(max_anc_w, 200)        
        notes_col_w = max(100, total_width - (fixed_id_width + name_col_w + nick_col_w + anc_col_w + 20))
        
        self.grid.SetColSize(0, fixed_id_width)
        self.grid.SetColSize(1, name_col_w)
        self.grid.SetColSize(2, nick_col_w)
        self.grid.SetColSize(3, anc_col_w)
        self.grid.SetColSize(4, notes_col_w)
        
        if event:
            event.Skip()

    def on_resize(self, event):
        self.update_grid_columns()
        event.Skip() 

    def update_grid(self, data):
        self.grid.ClearGrid()
        if self.grid.GetNumberRows() > 0:
            self.grid.DeleteRows(0, self.grid.GetNumberRows())
        self.grid.AppendRows(len(data))
        for i, row in enumerate(data):
            for j, val in enumerate(row):
                self.grid.SetCellValue(i, j, str(val) if val is not None else "")
        
    def load_data(self):
        self.db.cursor.execute("SELECT family_id, family_group, family_name, ancestral_family_name, family_notes FROM families")
        self.original_data = self.db.cursor.fetchall()
        self.update_grid(self.original_data)

    def on_add(self, event):
        if self.db.read_only: return
        self.grid.AppendRows(1)

    def on_delete(self, event):
        if self.db.read_only: return
        row = self.grid.GetGridCursorRow()
        f_id = self.grid.GetCellValue(row, 0)
        if f_id:
            self.db.cursor.execute("DELETE FROM families WHERE family_id = ?", (f_id,))
            self.db.conn.commit()
        self.grid.DeleteRows(row, 1)
        
    def on_save(self, event):
        if self.db.read_only: return
        for i in range(self.grid.GetNumberRows()):
            f_id = self.grid.GetCellValue(i, 0)
            f_group = self.grid.GetCellValue(i, 1)
            f_name = self.grid.GetCellValue(i, 2)
            f_ans = self.grid.GetCellValue(i, 3)
            f_notes = self.grid.GetCellValue(i, 4)
            
            try:
                if f_id and f_id.strip(): 
                    self.db.cursor.execute("""
                        UPDATE families 
                        SET family_group = ?, family_name = ?, family_notes = ?, ancestral_family_name = ?
                        WHERE family_id = ?
                    """, (f_group, f_name, f_notes, f_ans, f_id))
                else: 
                    self.db.cursor.execute("""
                        INSERT INTO families (family_group, family_name, ancestral_family_name, family_notes) 
                        VALUES (?, ?, ?, ?)
                    """, (f_group, f_name, f_ans, f_notes))
                    
                    # Update the grid with the newly generated ID
                    new_id = self.db.cursor.lastrowid
                    self.grid.SetCellValue(i, 0, str(new_id))
                    
            except Exception as e:
                error_msg = f"Failed to save Row {i + 1}:\n\n"
                error_msg += f"Family ID: {f_id if f_id else 'NEW'}\n"
                error_msg += f"Family Name: {f_name}\n\n"
                error_msg += f"Database Error: {str(e)}\n\n"
                error_msg += "Save aborted. Please fix the conflicting data and try again."
                
                wx.MessageBox(error_msg, "Save Error", wx.ICON_ERROR)
                self.db.conn.rollback()
                return
                     
        self.db.conn.commit()
        wx.MessageBox("Changes saved to database.", "Success")
    

    def on_sort_column(self, event):
        col = event.GetCol()
        data = []
        for i in range(self.grid.GetNumberRows()):
            row = [self.grid.GetCellValue(i, j) for j in range(self.grid.GetNumberCols())]
            data.append(row)
        
        def sort_key(row):
            val = row[col].strip()
            try:
                return float(val)
            except ValueError:
                return val.lower()

        data.sort(key=sort_key)
        
        if hasattr(self, '_last_col') and self._last_col == col and not getattr(self, '_desc', False):
            data.reverse()
            self._desc = True
        else:
            self._desc = False
        
        self._last_col = col
        
        self.grid.BeginBatch()
        for i, row in enumerate(data):
            for j, val in enumerate(row):
                self.grid.SetCellValue(i, j, val)
        self.grid.EndBatch()
        self.grid.ForceRefresh()
        
    def on_search(self, event):
        query = self.search_ctrl.GetValue().lower()
        filtered_data = [
            row for row in self.original_data 
            if any(query in str(cell).lower() for cell in row if cell is not None)
        ]        
        self.update_grid(filtered_data)    


class GenealogyFrame(wx.Frame):
    def __init__(self, engine_type, db_password, db="family.db", read_only=False, show_debug_panel=True):
        title_suffix = " [READ-ONLY MODE]" if read_only else ""
        super().__init__(None, title=f"Genealogy Pro{title_suffix}", size=(1150, 800))
        
        self.item_id_map = {} 
        self.tree_item_table = {} 
        self.tree_group_table = {} 
        
        self.db = GenealogyData(engine_type=engine_type, db_name=db, password=db_password, read_only=read_only)
        self.current_selected_id = None
        self.current_selected_family_id = None 
        self.io_helper = GenealogyIO(self.db)

        self.default_bmp = self.get_default_avatar()
        # Create a master vertical layout splitter to dock the console panel at the bottom base
        self.main_vertical_dock = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        
        # for side-by-side layout trees and profiles
        self.splitter = wx.SplitterWindow(self.main_vertical_dock, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        self.right_splitter = wx.SplitterWindow(self.splitter, style=wx.SP_3D)

        self.people_panel = wx.Panel(self.right_splitter) #
        self.people_sizer = wx.BoxSizer(wx.VERTICAL) #

        self.left_panel = wx.Panel(self.splitter)
        tree_sizer = wx.BoxSizer(wx.VERTICAL)
        self.left_sizer = tree_sizer
        
        self.people_label = wx.StaticText(self.people_panel, label="Member list") #
        self.people_sizer.Add(self.people_label, 0, wx.ALL, 10) #
        
        self.fm_list_view = wx.ListCtrl(self.people_panel, style=wx.LC_REPORT | wx.BORDER_SUNKEN) #
        self.fm_list_view.InsertColumn(0, "Name", width=250)  # Bumped column width slightly for names
        self.fm_list_view.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_fm_list_selection) #
        self.fm_list_view.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.on_fm_list_right_click) #
        
        # FIX: Explicitly pack the list control into the sizer with expanding stretch capabilities
        # proportion=1 means take up all remaining available vertical footprint space
        # flag=wx.EXPAND means stretch out horizontally to fit margins edge-to-edge
        self.people_sizer.Add(self.fm_list_view, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        
        # Commit the fully loaded vertical box sizer down onto the structural control panel
        self.people_panel.SetSizer(self.people_sizer)

        self.view_mode = wx.RadioBox(
            self.left_panel, label="View Mode", 
            choices=["Tree View", "People List", "Family List"],
            majorDimension=1, style=wx.RA_SPECIFY_ROWS
        )
        self.view_mode.Bind(wx.EVT_RADIOBOX, self.on_toggle_view)

        solid_pen = wx.Pen(wx.Colour(128, 128, 128), 1, wx.PENSTYLE_SOLID)
        self.tree = CT.CustomTreeCtrl(
            self.left_panel,
            agwStyle=(CT.TR_DEFAULT_STYLE | CT.TR_LINES_AT_ROOT) & ~CT.TR_NO_LINES & ~CT.TR_HAS_BUTTONS
        )
        
        self.tree.SetConnectionPen(solid_pen)
        self.tree._btnWidth = 0
        self.tree._btnHeight = 0
        self.tree.SetBackgroundColour(wx.WHITE) 
        self.tree.SetForegroundColour(wx.BLACK) 
        self.tree._linePen = wx.Pen(wx.BLACK, 1, wx.PENSTYLE_SOLID)
        
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_selection_changed)
        self.tree.Bind(wx.EVT_TREE_ITEM_RIGHT_CLICK, self.on_tree_right_click)
        self.tree.Bind(wx.EVT_TREE_BEGIN_DRAG, self.on_begin_drag)
        self.tree.Bind(wx.EVT_TREE_END_DRAG, self.on_end_drag)
        
        self.list_view = wx.ListCtrl(self.left_panel, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        self.list_view.InsertColumn(0, "Name", width=150)
        self.list_view.InsertColumn(1, "Family", width=100)
        self.list_view.InsertColumn(2, "Nickname", width=100) 
        self.list_view.InsertColumn(3, "Family ID", width=70)   
        self.list_view.InsertColumn(4, "ID", width=40)
        self.list_view.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_list_selection)
        self.list_view.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.on_list_right_click)
        self.list_view.Bind(wx.EVT_LIST_COL_CLICK, self.on_list_col_click)
        self.list_view.Hide()

        self.main_search = wx.SearchCtrl(self.left_panel, style=wx.TE_PROCESS_ENTER)
        self.main_search.SetHint("Search list...")
        self.main_search.Bind(wx.EVT_TEXT, self.on_main_search)
        self.main_search.Hide()

        self.list_sort_col = 0
        self.list_sort_desc = False
        self.current_list_data = []
       
        self.left_sizer.Add(self.tree, 1, wx.EXPAND | wx.ALL, 5)
        self.left_sizer.Add(self.main_search, 0, wx.EXPAND | wx.ALL, 5)
        self.left_sizer.Add(self.list_view, 1, wx.EXPAND | wx.ALL, 5)
        self.left_sizer.Add(self.view_mode, 0, wx.ALL, 5)
        self.left_panel.SetSizer(self.left_sizer)

        
        self.right_scrolled = wx.Panel(self.right_splitter)
        self.right_scrolled.SetBackgroundColour(wx.WHITE)
        self.container_sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.notebook = wx.Simplebook(self.right_scrolled)
        
        self.tab_person = wx.lib.scrolledpanel.ScrolledPanel(self.notebook)
        self.tab_family = wx.lib.scrolledpanel.ScrolledPanel(self.notebook)
        self.tab_person.SetBackgroundColour(wx.WHITE)
        self.tab_family.SetBackgroundColour(wx.WHITE)
        
        self.notebook.AddPage(self.tab_person, "Individual Profile")
        self.notebook.AddPage(self.tab_family, "Family Group Profile")
        
        self.container_sizer.Add(self.notebook, 1, wx.EXPAND)
        self.right_scrolled.SetSizer(self.container_sizer)

        self.person_sizer = wx.BoxSizer(wx.VERTICAL)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        # self.name_display = wx.StaticText(self.tab_person, label="Select a Person")
        # font = self.name_display.GetFont()
        # font.SetPointSize(16); font.SetWeight(wx.FONTWEIGHT_BOLD)
        # self.name_display.SetFont(font)

        profile_sizer = wx.BoxSizer(wx.HORIZONTAL)
        left_column = wx.BoxSizer(wx.VERTICAL)
        self.photo_ctrl = wx.StaticBitmap(self.tab_person, bitmap=self.get_default_avatar())
        self.photo_ctrl.SetCursor(wx.Cursor(wx.CURSOR_HAND)) # Shows the pointing finger on hover
        self.photo_ctrl.SetToolTip("Click to update photo")
        self.photo_ctrl.Bind(wx.EVT_LEFT_DOWN, self.on_change_photo)
        
        self.name_label = wx.StaticText(self.tab_person, label="Name")
        name_font = self.name_label.GetFont()
        name_font.MakeBold()
        name_font.SetPointSize(12)
        self.name_label.SetFont(name_font)
        
        # Stack photo and name vertically, centered
        left_column.Add(self.photo_ctrl, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 5)
        left_column.Add(self.name_label, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 5)
        
        
        # --- RIGHT COLUMN: vCard QR Code ---
        right_column = wx.BoxSizer(wx.VERTICAL)
        
        self.qr_ctrl = wx.StaticBitmap(self.tab_person, bitmap=self.generate_vcard_qr("name"))
        
        right_column.Add(self.qr_ctrl, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 5)
        
        
        # --- ASSEMBLE ---
        # Add left column, push right column to the far edge using AddStretchSpacer
        profile_sizer.Add(left_column, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 10)
        profile_sizer.AddStretchSpacer(1) 
        profile_sizer.Add(right_column, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 10)        
        if self.db.read_only:
            self.btn_photo.Disable()
        
        # header_sizer.Add(self.name_display, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 15)
        header_sizer.Add(profile_sizer, 0, wx.ALL, 15)
        self.person_sizer.Add(header_sizer, 0, wx.EXPAND)

        self.info_sizer = wx.StaticBoxSizer(wx.VERTICAL, self.tab_person, "Personal Details")
        self.details_grid = wx.FlexGridSizer(cols=2, vgap=10, hgap=10)
        self.details_grid.AddGrowableCol(1, 1)
        
        self.fields = {}
        field_list = [
            ("id", "System ID (Read-only)"),
            ("name", "Full Name"),
            ("surname", "Surname"),
            ("dob", "Date of Birth"),
            ("nicknames", "Nicknames"),        
            ("other_names", "Other Names"),
            ("sex", "Sex"),
            ("deceased", "Deceased"),
            ("family_name", "Family nickname"),
            ("family_id", "Family ID"),
            ("family_group", "Family Group"),
            ("ancestral_family_name", "Ancestral Family"),
            ("father_name", "Father (ID or Name)"),
            ("mother_name", "Mother (ID or Name)"), 
            ("husband_names", "Husbands (IDs or Names)"),
            ("wife_names", "Wives (IDs or Names)"),
            ("son_names", "Sons (IDs or Names)"),
            ("daughter_names", "Daughters (IDs or Names)"),
            ("religion", "Religion"),
            ("caste", "Caste"),
            ("location", "Location"),
            ("phone", "Primary Phone"),
            ("other_phones", "Other Phones"),
            ("whatsapp", "WhatsApp"),
            ("telegram", "Telegram"),            
            ("gmail", "Gmail"),
            ("google_photos", "Google Photos Link"),
            ("homepage", "Home Page"),
            ("facebook", "Facebook"),
            ("instagram", "Instagram"),
            ("home_address", "Home Address"),
            ("home_contact", "Home Contact"),
            ("office_address", "Office Address"),
            ("office_contact", "Office Contact"),
            ("local_photo_path", "Local Photo Path"),
            ("notes", "Notes"),
            ("conf_notes","Confidential Notes")
        ]
        
        for key, label in field_list:
            lbl = wx.StaticText(self.tab_person, label=label)
            
            # v19.56: Ensure conf_notes renders as a large multiline box
            is_multiline = key in ["notes", "conf_notes", "home_address", "office_address", "other_phones"]
            
            style = wx.TE_READONLY if (key == "id" or self.db.read_only) else 0
            if is_multiline:
                style |= wx.TE_MULTILINE
            elif not self.db.read_only and key != "id":
                style |= wx.TE_PROCESS_ENTER
                
            txt = wx.TextCtrl(self.tab_person, style=style)
            
            # Only bind the automatic "Enter to Save" event to single-line text boxes
            if key != "id" and not self.db.read_only and not is_multiline:
                txt.Bind(wx.EVT_TEXT_ENTER, self.on_save_details)
            
            if style & wx.TE_MULTILINE:
                txt.SetMinSize((-1, 100)) 
                # Give Confidential Notes a distinct color to remind the user it is private
                if key == "conf_notes":
                    txt.SetBackgroundColour(wx.Colour(255, 245, 245)) # Light red tint
                self.details_grid.Add(lbl, 0, wx.ALIGN_TOP | wx.TOP, 5)
                self.details_grid.Add(txt, 1, wx.EXPAND | wx.ALL, 5)
            else:
                self.details_grid.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
                self.details_grid.Add(txt, 1, wx.EXPAND | wx.ALL, 5)
            self.fields[key] = txt
        self.info_sizer.Add(self.details_grid, 1, wx.EXPAND | wx.ALL, 10)
        self.btn_save = wx.Button(self.tab_person, label="Update Record")
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save_details)
        self.info_sizer.Add(self.btn_save, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        self.person_sizer.Add(self.info_sizer, 1, wx.EXPAND | wx.ALL, 10)

        self.add_field_btn = wx.Button(self.tab_person, label="Add Custom Field")
        self.add_field_btn.Bind(wx.EVT_BUTTON, self.on_add_custom_field)
        self.person_sizer.Add(self.add_field_btn, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        
        if self.db.read_only:
            self.btn_save.Disable()
            self.add_field_btn.Disable()
        
        self.tab_person.SetSizer(self.person_sizer)
        self.tab_person.SetupScrolling(scroll_x=False, scroll_y=True, rate_y=15)

        # Define New Unique Navigation Reference IDs
        self.ID_NEW_DB = wx.NewIdRef()
        self.ID_OPEN_DB = wx.NewIdRef()
        self.ID_REFRESH = wx.NewIdRef()
        self.ID_ED_PEOPLE = wx.NewIdRef()
        self.ID_ED_FAMILIES = wx.NewIdRef()
        self.ID_REMOVE_DUPLICATES = wx.NewIdRef()
        self.ID_REPORT = wx.NewIdRef() 
        self.ID_FIND_RELATIONSHIP = wx.NewIdRef()
        self.ID_FIND_RELATIVES = wx.NewIdRef()
        self.ID_TOGGLE_DEBUG_PANEL = wx.NewIdRef() #
        
        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        
        # Core File Actions
        new_db_item = file_menu.Append(wx.ID_NEW, "&New Database...\tCtrl+N", "Create a brand new genealogy database")        
        open_db_item = file_menu.Append(wx.ID_OPEN, "Open Database...", "Open a different genealogy database")
        file_menu.AppendSeparator()
        
        # Submenu: File -> Import
        import_submenu = wx.Menu()
        import_sql_item = import_submenu.Append(wx.ID_ANY, "SQL Dump...")
        import_csv_item = import_submenu.Append(wx.ID_ANY, "CSV...")
        import_json_item = import_submenu.Append(wx.ID_ANY, "JSON...")
        import_gedcom_item = import_submenu.Append(wx.ID_ANY, "GEDCOM...")
        import_vcard_item = import_submenu.Append(wx.ID_ANY, "vCard...")
        file_menu.AppendSubMenu(import_submenu, "Import")
        
        # Submenu: File -> Export
        export_submenu = wx.Menu()
        export_sql_item = export_submenu.Append(wx.ID_ANY, "SQL Dump...")
        export_csv_item = export_submenu.Append(wx.ID_ANY, "CSV...")
        export_json_item = export_submenu.Append(wx.ID_ANY, "JSON...")
        export_gedcom_item = export_submenu.Append(wx.ID_ANY, "GEDCOM...")
        export_vcard_item = export_submenu.Append(wx.ID_ANY, "vCard...")
        export_report_item = export_submenu.Append(wx.ID_ANY, "HTML/TeX Report\tCtrl+E")
        file_menu.AppendSubMenu(export_submenu, "Export")
        
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, "E&xit\tAlt+X")

        tools_menu = wx.Menu()
        tools_menu.Append(self.ID_NEW_DB, "New Database...\tCtrl+N", "Create a completely fresh database file")
        tools_menu.Append(self.ID_OPEN_DB, "Open Database...\tCtrl+O", "Open database file")
        tools_menu.AppendSeparator()
        tools_menu.Append(self.ID_REFRESH, "Refresh\tF5", "Refresh tracking loops")
        tools_menu.AppendSeparator()
        tools_menu.Append(self.ID_ED_PEOPLE, "Edit People...", "Edit individual profile fields directly")
        tools_menu.Append(self.ID_ED_FAMILIES, "Edit Families...", "Edit macro lineage structures")
        tools_menu.Append(self.ID_REMOVE_DUPLICATES, "Remove Duplicates...", "Scan records and merge identical individual names")
        tools_menu.AppendSeparator()
        tools_menu.Append(self.ID_REPORT, "Generate Report", "Generate HTML/TeX report for the active selection or entire database")
        tools_menu.AppendSeparator()
        tools_menu.Append(self.ID_FIND_RELATIONSHIP, "Find Relationship...", "Trace separation degrees between two targets")
        tools_menu.Append(self.ID_FIND_RELATIVES, "Find Relatives...", "Filter specific kin categories by keyword rules")
        tools_menu.AppendSeparator()

        debug_menu_item = tools_menu.AppendCheckItem(self.ID_TOGGLE_DEBUG_PANEL, "Toggle Debug Shell", "Toggle bottom workspace terminal box")
        debug_menu_item.Check(show_debug_panel)       
        
        menubar.Append(file_menu, "&File")
        menubar.Append(tools_menu, "&Tools")        
        self.SetMenuBar(menubar)
        
        # Bind Menu Commands
        self.Bind(wx.EVT_MENU, self.on_open_db, open_db_item)
        self.Bind(wx.EVT_MENU, self.on_import_sql, import_sql_item)
        self.Bind(wx.EVT_MENU, self.on_import_csv, import_csv_item)
        self.Bind(wx.EVT_MENU, self.on_import_json, import_json_item)
        self.Bind(wx.EVT_MENU, self.on_import_gedcom, import_gedcom_item)
        self.Bind(wx.EVT_MENU, self.on_import_vcard_ui, import_vcard_item)
        
        self.Bind(wx.EVT_MENU, self.on_export_sql, export_sql_item)
        self.Bind(wx.EVT_MENU, self.on_export_csv, export_csv_item)
        self.Bind(wx.EVT_MENU, self.on_export_json, export_json_item)
        self.Bind(wx.EVT_MENU, self.on_export_gedcom, export_gedcom_item)
        self.Bind(wx.EVT_MENU, self.on_export_vcard_ui, export_vcard_item)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_generate_full_report(), export_report_item)
        self.Bind(wx.EVT_MENU, self.on_exit_app, exit_item)
        
        # Handle Read-Only constraints dynamically
        if self.db.read_only:
            for item in [import_csv_item, import_json_item, import_gedcom_item, import_vcard_item, import_sql_item]:
                item.Enable(False)    
        

        self.toolbar = self.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_TEXT)
        

        
        # Populate Tools using standard system art layouts
        self.toolbar.AddTool(self.ID_NEW_DB, "New DB", wx.ArtProvider.GetBitmap(wx.ART_NEW, wx.ART_TOOLBAR, (16,16)), "Create a completely fresh database file")
        self.toolbar.AddTool(self.ID_OPEN_DB, "Open DB", wx.ArtProvider.GetBitmap(wx.ART_FILE_OPEN, wx.ART_TOOLBAR, (16,16)), "Open database file")        
        self.toolbar.AddTool(self.ID_REFRESH, "Refresh", wx.ArtProvider.GetBitmap(wx.ART_REDO, wx.ART_TOOLBAR, (16,16)), "Refresh tracking loops")
        self.toolbar.AddSeparator()
        self.toolbar.AddTool(self.ID_ED_PEOPLE, "Ed People", wx.ArtProvider.GetBitmap(wx.ART_LIST_VIEW, wx.ART_TOOLBAR, (16,16)), "Edit individual profile fields directly")
        self.toolbar.AddTool(self.ID_ED_FAMILIES, "Ed Families", wx.ArtProvider.GetBitmap(wx.ART_FOLDER, wx.ART_TOOLBAR, (16,16)), "Edit macro lineage structures")
        self.toolbar.AddTool(self.ID_REMOVE_DUPLICATES, "Remove Duplicates", wx.ArtProvider.GetBitmap(wx.ART_DELETE, wx.ART_TOOLBAR, (16,16)), "Scan records and merge identical individual names")        
        self.toolbar.AddSeparator()
        self.toolbar.AddTool(self.ID_REPORT, "Report", wx.ArtProvider.GetBitmap(wx.ART_PRINT, wx.ART_TOOLBAR, (16,16)), "Generate HTML/TeX report for the active selection or entire database")
        self.toolbar.AddSeparator() 
        self.toolbar.AddTool(self.ID_FIND_RELATIONSHIP, "Find Relationship", wx.ArtProvider.GetBitmap(wx.ART_FIND, wx.ART_TOOLBAR, (16,16)), "Trace separation degrees between two targets")
        self.toolbar.AddTool(self.ID_FIND_RELATIVES, "Find Relatives", wx.ArtProvider.GetBitmap(wx.ART_HELP_SIDE_PANEL, wx.ART_TOOLBAR, (16,16)), "Filter specific kin categories by keyword rules")
        self.toolbar.AddSeparator()
        
        # Keep your checkbox debug controller from v19.86
        self.toolbar.AddCheckTool(self.ID_TOGGLE_DEBUG_PANEL, "Debug Shell", wx.ArtProvider.GetBitmap(wx.ART_REPORT_VIEW, wx.ART_TOOLBAR, (16,16)), wx.NullBitmap, "Toggle bottom workspace terminal box")
        self.toolbar.ToggleTool(self.ID_TOGGLE_DEBUG_PANEL, show_debug_panel) #
        
        # Bind Actions
        self.Bind(wx.EVT_TOOL, self.on_refresh_tree, id=self.ID_REFRESH)
        self.Bind(wx.EVT_TOOL, self.on_edit_people_action, id=self.ID_ED_PEOPLE)
        self.Bind(wx.EVT_TOOL, self.on_edit_families_action, id=self.ID_ED_FAMILIES)
        self.Bind(wx.EVT_TOOL, self.on_remove_duplicates, id=self.ID_REMOVE_DUPLICATES)
        self.Bind(wx.EVT_TOOL, self.on_toolbar_report_action, id=self.ID_REPORT)        
        self.Bind(wx.EVT_TOOL, self.on_find_relationship_ui, id=self.ID_FIND_RELATIONSHIP)
        self.Bind(wx.EVT_TOOL, self.on_find_relatives_toolbar_action, id=self.ID_FIND_RELATIVES)
        self.Bind(wx.EVT_TOOL, self.on_toggle_debug_panel_tool, id=self.ID_TOGGLE_DEBUG_PANEL) #
        
        self.toolbar.Realize()

        self.Bind(wx.EVT_MENU, self.on_new_db, new_db_item)
        self.Bind(wx.EVT_TOOL, self.on_new_db, id=self.ID_NEW_DB)
        self.Bind(wx.EVT_MENU, self.on_open_db, open_db_item)
        self.Bind(wx.EVT_TOOL, self.on_open_db, id=self.ID_OPEN_DB)
        self.Bind(wx.EVT_MENU, self.on_exit_app, exit_item)
        
        self.family_sizer = wx.BoxSizer(wx.VERTICAL)
        self.family_title_text = wx.StaticText(self.tab_family, label="Family Profile Segment Details")
        font_f = self.family_title_text.GetFont()
        font_f.SetPointSize(16); font_f.SetWeight(wx.FONTWEIGHT_BOLD)
        self.family_title_text.SetFont(font_f)
        self.family_sizer.Add(self.family_title_text, 0, wx.ALL, 15)

        self.family_info_sizer = wx.StaticBoxSizer(wx.VERTICAL, self.tab_family, "Family Group Profile")
        self.family_grid = wx.FlexGridSizer(cols=2, vgap=10, hgap=10)
        self.family_grid.AddGrowableCol(1, 1)

        self.fam_lbl_id = wx.StaticText(self.tab_family, label="Family ID:")
        self.fam_txt_id = wx.TextCtrl(self.tab_family, style=wx.TE_READONLY)
        self.fam_lbl_name = wx.StaticText(self.tab_family, label="Family Name:")
        self.fam_txt_name = wx.TextCtrl(self.tab_family, style=wx.TE_READONLY if self.db.read_only else wx.TE_PROCESS_ENTER)        
        if not self.db.read_only: self.fam_txt_name.Bind(wx.EVT_TEXT_ENTER, self.on_save_family_details)
        
        self.fam_lbl_group = wx.StaticText(self.tab_family, label="Family Group:")
        self.fam_txt_group = wx.TextCtrl(self.tab_family, style=wx.TE_READONLY if self.db.read_only else wx.TE_PROCESS_ENTER)        
        if not self.db.read_only: self.fam_txt_group.Bind(wx.EVT_TEXT_ENTER, self.on_save_family_details)

        self.fam_lbl_aname = wx.StaticText(self.tab_family, label="Ancestral Family Name:")
        self.fam_txt_aname = wx.TextCtrl(self.tab_family, style=wx.TE_READONLY if self.db.read_only else wx.TE_PROCESS_ENTER)        
        if not self.db.read_only: self.fam_txt_aname.Bind(wx.EVT_TEXT_ENTER, self.on_save_family_details)
        self.fam_lbl_notes = wx.StaticText(self.tab_family, label="Notes:")
        self.fam_txt_notes = wx.TextCtrl(self.tab_family, style=wx.TE_MULTILINE | (wx.TE_READONLY if self.db.read_only else 0))
        self.fam_txt_notes.SetMinSize((-1, 200))
        
        self.family_grid.Add(self.fam_lbl_id, 0, wx.ALIGN_CENTER_VERTICAL)
        self.family_grid.Add(self.fam_txt_id, 1, wx.EXPAND | wx.ALL, 5)
        self.family_grid.Add(self.fam_lbl_name, 0, wx.ALIGN_CENTER_VERTICAL)
        self.family_grid.Add(self.fam_txt_name, 1, wx.EXPAND | wx.ALL, 5)
        self.family_grid.Add(self.fam_lbl_group, 0, wx.ALIGN_CENTER_VERTICAL)
        self.family_grid.Add(self.fam_txt_group, 1, wx.EXPAND | wx.ALL, 5)
        self.family_grid.Add(self.fam_lbl_aname, 0, wx.ALIGN_CENTER_VERTICAL)
        self.family_grid.Add(self.fam_txt_aname, 1, wx.EXPAND | wx.ALL, 5)
        self.family_grid.Add(self.fam_lbl_notes, 0, wx.ALIGN_TOP | wx.TOP, 5)
        self.family_grid.Add(self.fam_txt_notes, 1, wx.EXPAND | wx.ALL, 5)
        self.family_info_sizer.Add(self.family_grid, 1, wx.EXPAND | wx.ALL, 10)
        
        self.btn_fam_save = wx.Button(self.tab_family, label="Update Family")
        self.btn_fam_save.Bind(wx.EVT_BUTTON, self.on_save_family_details)
        self.family_info_sizer.Add(self.btn_fam_save, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        self.family_sizer.Add(self.family_info_sizer, 1, wx.EXPAND | wx.ALL, 10)
        
        if self.db.read_only:
            self.btn_fam_save.Disable()
        
        self.tab_family.SetSizer(self.family_sizer)
        self.tab_family.SetupScrolling(scroll_x=False, scroll_y=True, rate_y=15)

        self.right_splitter.SplitVertically(self.people_panel, self.right_scrolled, 300)
        self.splitter.SplitVertically(self.left_panel, self.right_splitter, 300)
        self.splitter.SetMinimumPaneSize(100)

        # Always build the debugger panel so it remains accessible in the background variable namespace
        self.build_debugger_console_bar()
        self.debugger_panel_is_visible = show_debug_panel
        
        # CONFIGURATION PASS FOR v19.82: Conditional Layout Docking
        if show_debug_panel:
            # Display normally by splitting the vertical view workspace window
            self.main_vertical_dock.SplitHorizontally(self.splitter, self.debugger_panel, -150)
        else:
            # Initialize the interface tree and profile panes across the full screen space, leaving console un-split
            self.main_vertical_dock.Initialize(self.splitter)
            self.debugger_panel.Hide()
            
        self.main_vertical_dock.SetMinimumPaneSize(50)
        
        dprint("restore this refresh_tree()")
        self.refresh_tree()

    def get_default_avatar(self):
        """Generates a default 150x150 placeholder avatar bitmap."""
        # Create a blank 150x150 image (matches the size of our custom photos)
        img = wx.Image(150, 150, clear=True)
        
        # Fill it with a generic light gray background (RGB: 220, 220, 220)
        img.SetRGB(wx.Rect(0, 0, 150, 150), 220, 220, 220)
        
        # Convert the raw Image data into a UI-ready Bitmap
        return wx.Bitmap(img)
        
    def on_change_photo(self, event):
        """Triggered when the user clicks directly on the profile picture."""
        with wx.FileDialog(self, "Select new profile photo", wildcard="Image files (*.jpg;*.png)|*.jpg;*.png", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                new_image_path = dlg.GetPath()
                
                # Load and scale the new image
                img = wx.Image(new_image_path, wx.BITMAP_TYPE_ANY)
                img = img.Scale(150, 150, wx.IMAGE_QUALITY_HIGH) # Adjust size to fit your UI
                self.photo_ctrl.SetBitmap(wx.Bitmap(img))
                self.Layout() # Force UI refresh
                
                # TODO: Save the new file path to your database
                
        # event.Skip() is critical here so wxPython doesn't freeze the mouse focus
        event.Skip() 

    def generate_vcard_qr(self, name):
        """Generates a vCard QR code and returns it directly as a wx.Bitmap."""
        import qrcode
        import io
        
        # Standard vCard formatting
        vcard_data = f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nEND:VCARD"
        
        # Generate the QR Code using PIL
        qr = qrcode.QRCode(box_size=4, border=2)
        qr.add_data(vcard_data)
        qr.make(fit=True)
        pil_img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert the PIL image to a wx.Bitmap in memory without saving to disk
        with io.BytesIO() as byte_stream:
            pil_img.save(byte_stream, format="PNG")
            byte_stream.seek(0)
            wx_img = wx.Image(byte_stream, wx.BITMAP_TYPE_PNG)
            
        return wx.Bitmap(wx_img)

        
    def on_new_db(self, event):
        """Creates a completely fresh database file, formats it, and updates application state."""
        # Use FD_SAVE and FD_OVERWRITE_PROMPT to ensure safe file creation behaviors
        with wx.FileDialog(self, "Create New Database", wildcard="SQLite DB files (*.db)|*.db", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                new_db_path = dlg.GetPath()
                
                # Auto-append extension if the user forgot to type it
                if not new_db_path.lower().endswith('.db'):
                    new_db_path += '.db'
                    
                # Close the currently active database connection cleanly
                try:
                    self.db.conn.close()
                except Exception as e:
                    dprint(f"Warning closing current db: {e}")
                
                # Reinitialize connection against the new file path. 
                # Your engine will automatically build the empty table schemas.
                # Note: We force read_only=False because you cannot create a new DB in view-only mode!
                self.db = GenealogyData(db=new_db_path, password=None, read_only=False) 
                self.io_helper = GenealogyIO(self.db)
                
                # Persist the change to configs so the app boots to this file next time
                engine = getattr(self, 'engine_type', 'sqlite3')
                update_saved_settings(new_db_path, engine)
                
                # Reset UI states
                self.current_selected_id = None
                self.current_selected_family_id = None
                self.refresh_tree()
                
                wx.MessageBox(f"Successfully created and mounted new database:\n{new_db_path}", "Database Created", wx.OK | wx.ICON_INFORMATION)
                wx.LogStatus(f"Active database set to: {new_db_path}")
        
    def on_toolbar_report_action(self, event):
        """Resolves the active UI selection context and dynamically triggers the appropriate report scope."""
        target_group = None
        target_family = None
        
        mode = self.view_mode.GetSelection()

        # Scenario 1: User is operating inside the Tree View mode
        if mode == 0:  
            sel = self.tree.GetSelection()
            # If a valid specific node is selected (and it is not the global "All Families" root)
            if sel and sel.IsOk() and sel != self.root_item:
                node_data = self.tree.GetItemData(sel)
                if node_data:
                    # Determine if this node is a sub-branch (family_name) or a top-level folder (family_group)
                    self.db.cursor.execute("SELECT family_group FROM families WHERE family_name = ?", (node_data,))
                    res = self.db.cursor.fetchone()
                    
                    if res:
                        # It is a specific sub-branch. Isolate the family and its parent group.
                        target_group = res[0] if res[0] else node_data
                        target_family = node_data
                    else:
                        # If it is not found as a family_name, it must be a top-level Group bucket.
                        target_group = node_data

        # Scenario 2: User is operating inside the Family List mode
        elif mode == 2:  
            if getattr(self, 'current_selected_family_id', None):
                self.db.cursor.execute("SELECT family_group, family_name FROM families WHERE family_id = ?", (self.current_selected_family_id,))
                res = self.db.cursor.fetchone()
                if res:
                    target_group = res[0]
                    target_family = res[1]

        # Scenario 3: If no valid selection was made, or the user clicked the root "All Families" node,
        # or the user is in "People List" mode (where families aren't explicitly selected), 
        # both variables safely remain `None`, which triggers a global database report by default.
        
        # Hand off the resolved parameters to the report engine
        self.ctx_generate_full_report(target_group=target_group, target_family=target_family)


    def on_toggle_debug_panel_tool(self, event):
        """Docks or detaches the debugger text engine dynamically from the primary view frame layout."""
        is_checked = event.IsChecked()
        
        if is_checked and not self.debugger_panel_is_visible:
            # Unhide and dock back down into a split horizonal pane layout orientation
            self.debugger_panel.Show()
            self.main_vertical_dock.SplitHorizontally(self.splitter, self.debugger_panel, -150)
            self.debugger_panel_is_visible = True
            
        elif not is_checked and self.debugger_panel_is_visible:
            # Detach the layout split pane and pull workspace up to full layout display sizes
            self.main_vertical_dock.Unsplit(self.debugger_panel)
            self.debugger_panel.Hide()
            self.debugger_panel_is_visible = False
            
        self.main_vertical_dock.Layout()

    def on_find_relatives_toolbar_action(self, event):
        """Launches a lookup modal to extract every single relative matching 

        a keyword relation type and filters the primary People List display panel.
        """
        base_person_lbl = f"ID: {self.current_selected_id}" if self.current_selected_id else ""
        
        dlg = wx.Dialog(self, title="Filter Relatives by Keyword", size=(400, 220))
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        # Field 1: Target Selector Identification
        hbox1 = wx.BoxSizer(wx.HORIZONTAL)
        lbl1 = wx.StaticText(dlg, label="Base Person (Name or ID):")
        txt_person = wx.TextCtrl(dlg, value=base_person_lbl)
        hbox1.Add(lbl1, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        hbox1.Add(txt_person, 1, wx.ALL | wx.EXPAND, 5)
        
        # Field 2: Target Relationship Type Keyword
        hbox2 = wx.BoxSizer(wx.HORIZONTAL)
        lbl2 = wx.StaticText(dlg, label="Relationship Keyword:\n(e.g. cousin, uncle, sibling)")
        txt_keyword = wx.TextCtrl(dlg, value="")
        txt_keyword.SetHint("Type kinship term to extract...")
        hbox2.Add(lbl2, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        hbox2.Add(txt_keyword, 1, wx.ALL | wx.EXPAND, 5)
        
        btn_sizer = dlg.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(hbox1, 0, wx.EXPAND | wx.ALL, 10)
        vbox.Add(hbox2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        vbox.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        dlg.SetSizer(vbox)
        dlg.Layout()
        
        if dlg.ShowModal() == wx.ID_OK:
            person_query = txt_person.GetValue().strip()
            keyword_query = txt_keyword.GetValue().strip().lower()
            
            if not person_query or not keyword_query:
                wx.MessageBox("Both lookup parameters are mandatory.", "Filter Canceled", wx.ICON_WARNING)
                dlg.Destroy()
                return
                
            # Resolve the raw string pointer down to a solid base integer row ID
            base_resolved_id = None
            if "(" in person_query and ")" in person_query:
                try: base_resolved_id = int(person_query.split("(")[-1].replace(")", "").strip())
                except: pass
            elif person_query.isdigit():
                base_resolved_id = int(person_query)
            else:
                self.db.cursor.execute("SELECT id FROM people WHERE name = ?", (person_query,))
                res = self.db.cursor.fetchone()
                if res: base_resolved_id = res[0]
                
            if not base_resolved_id:
                wx.MessageBox(f"Could not locate a record matching '{person_query}' inside tracking profiles.", "Lookup Error", wx.ICON_ERROR)
                dlg.Destroy()
                return
                
            # Execute the cross-network graph tracing lookup loop
            self.execute_relatives_filter_pipeline(base_resolved_id, keyword_query)
            
        dlg.Destroy()

    def execute_relatives_filter_pipeline(self, base_id, keyword):
        """Forces the left notebook panel down to 'People List' view mode, filters rows 

        dynamically via NetworkX kinship generation string maps, and repopulates list_view.
        """
        # 1. Flip the View Mode radio box choice tracker to 'People List' (Index position 1)
        self.view_mode.SetSelection(1)
        self.on_toggle_view(None) # Force layout panel visibility toggles to engage cleanly
        
        # 2. Extract every entry row matching criteria
        self.db.cursor.execute("SELECT name, family_group, family_name, family_id, id FROM people")
        all_entries = self.db.cursor.fetchall()
        
        filtered_matches = []
        
        for name, fg, fn, f_id, p_id in all_entries:
            if p_id == base_id:
                continue
                
            # Leverage your internal path separation network loop mapping
            success, kinship_rep, _, _, _ = self.find_relationship(base_id, p_id)
            
            if success and kinship_rep:
                clean_term = kinship_rep.replace("Calculated Relationship:", "").strip().lower()
                # Check if user keyword matches the calculated kinship term
                if keyword in clean_term:
                    filtered_matches.append((name, fg, fn, f_id, p_id))
                    
        # 3. Inject the results into the list_view container layout
        self.list_view.DeleteAllItems() #
        self.current_list_data = filtered_matches # Override list scope parameter tracking array
        
        if not filtered_matches:
            wx.MessageBox(f"Found 0 family members matching the category filter keyword '{keyword}' relative to this individual.", "Filter Completed")
            return
            
        for idx, row in enumerate(filtered_matches):
            self.list_view.InsertItem(idx, str(row[0] if row[0] is not None else "")) # Name
            self.list_view.SetItem(idx, 1, str(row[1] if row[1] is not None else "")) # Family
            self.list_view.SetItem(idx, 2, str(row[2] if row[2] is not None else "")) # Nickname
            self.list_view.SetItem(idx, 3, str(row[3] if row[3] is not None else "")) # Family ID
            self.list_view.SetItem(idx, 4, str(row[4] if row[4] is not None else "")) # ID
            self.list_view.SetItemData(idx, row[4]) #
        
    def build_debugger_console_bar(self):
        """Constructs a unified, single-pane terminal window mimicking a native interactive Python shell."""
        import code
        
        self.debugger_panel = wx.Panel(self.main_vertical_dock)
        self.debugger_panel.SetBackgroundColour(wx.Colour(240, 242, 245))
        
        panel_vbox = wx.BoxSizer(wx.VERTICAL)
        
        console_lbl = wx.StaticText(self.debugger_panel, label="Unified Python Interactive Shell Console")
        font = console_lbl.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        console_lbl.SetFont(font)
        
        # Context Environment Definitions mapped directly into interpreter runtime variables
        self.shell_locals = {
            'frame': self,
            'db': self.db,
            'io': self.io_helper,
            'wx': wx,
            'current_id': getattr(self, 'current_selected_id', None)
        }
        
        # Initialize the console engine
        self.console_shell = code.InteractiveConsole(locals=self.shell_locals)
        
        # v19.84 CHANGE: Single, combined Read/Write terminal control block
        self.terminal = wx.TextCtrl(self.debugger_panel, style=wx.TE_MULTILINE | wx.TE_RICH | wx.TE_PROCESS_ENTER)
        self.terminal.SetBackgroundColour(wx.Colour(30, 30, 30))
        self.terminal.SetForegroundColour(wx.Colour(240, 240, 240))
        self.terminal.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        
        # Write welcome message and seed the initial terminal prompt
        self.terminal.AppendText("Python Interactive Shell Environment Loaded. Globals: 'frame', 'db', 'io'\n>>> ")
        
        # Track the exact character position where the current user-input prompt zone begins
        self.prompt_start_pos = self.terminal.GetLastPosition()
        
        # Bind keystroke actions to control custom cursor evaluations
        self.terminal.Bind(wx.EVT_KEY_DOWN, self.on_terminal_key_down)
        
        panel_vbox.Add(console_lbl, 0, wx.ALL, 5)
        panel_vbox.Add(self.terminal, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        
        self.debugger_panel.SetSizer(panel_vbox)

    def on_terminal_key_down(self, event):
        """Intercepts keyboard inputs to process inline commands at the active terminal prompt."""
        key_code = event.GetKeyCode()
        
        # Process command execution upon encountering a standard Return key
        if key_code in [wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER]:
            total_text_length = self.terminal.GetLastPosition()
            
            # Isolate only the code string written after the prompt marker
            command_line = self.terminal.GetRange(self.prompt_start_pos, total_text_length)
            
            # Append a newline to visually shift down the display carriage
            self.terminal.AppendText("\n")
            
            # Sync context handles
            self.shell_locals['current_id'] = self.current_selected_id
            
            import sys
            from io import StringIO
            
            # Temporarily trap standard system print outputs
            old_stdout, old_stderr = sys.stdout, sys.stderr
            captured_stdout = sys.stdout = StringIO()
            captured_stderr = sys.stderr = StringIO()
            
            # Push the line down to the incremental execution state compiler
            needs_more_input = self.console_shell.push(command_line)
            
            # Restore pipeline environments
            sys.stdout, sys.stderr = old_stdout, old_stderr
            
            # Extract standard execution trace dump writeouts
            output_logs = captured_stdout.getvalue()
            error_logs = captured_stderr.getvalue()
            
            if output_logs:
                self.terminal.AppendText(output_logs)
            if error_logs:
                self.terminal.AppendText(error_logs)
                
            # Determine correct prefix prompt text to inject next
            prompt_marker = "... " if needs_more_input else ">>> "
            self.terminal.AppendText(prompt_marker)
            
            # Advance the tracking bookmark index pointer up to the fresh baseline edge
            self.prompt_start_pos = self.terminal.GetLastPosition()
            self.terminal.ShowPosition(self.prompt_start_pos)
            
            # Veto the event so wxTextCtrl doesn't inject a second trailing blank newline line
            return
            
        # Safety constraint: Prevent users from backspacing out the active '>>> ' or '... ' text markers
        elif key_code == wx.WXK_BACK:
            current_cursor_pos = self.terminal.GetInsertionPoint()
            if current_cursor_pos <= self.prompt_start_pos:
                # Discard keystroke action pass
                return
                
        event.Skip()

    def on_edit_people_action(self, event):
        self.on_edit_people(event)
    
    def on_edit_families_action(self, event):
        self.on_edit_families(event)
        
    def on_import_db(self, event):
        if self.db.read_only: return
        with wx.FileDialog(self, "Select Unencrypted Database", wildcard="DB files (*.db)|*.db", style=wx.FD_OPEN) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                source_path = dlg.GetPath()
                if wx.MessageBox("This will import and encrypt the data. Proceed?", "Confirm Import", wx.YES_NO) == wx.YES:
                    success, msg = self.io_helper.import_unencrypted_sqlite(source_path)
                    if success:
                        self.refresh_tree() 
                        wx.MessageBox(msg, "Import Success", wx.OK | wx.ICON_INFORMATION)
                    else:
                        wx.MessageBox(msg, "Import Error", wx.OK | wx.ICON_ERROR)
        
    def on_export_db(self, event):
        with wx.FileDialog(self, "Export Unencrypted Database", wildcard="DB files (*.db)|*.db", style=wx.FD_SAVE) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                target_path = dlg.GetPath()
                success, msg = self.io_helper.export_to_unencrypted_sqlite(target_path)
                if success:
                    wx.MessageBox(msg, "Export Success", wx.OK | wx.ICON_INFORMATION)
                else:
                    wx.MessageBox(msg, "Export Error", wx.OK | wx.ICON_ERROR)

    def on_fm_list_right_click(self, event):
        if self.db.read_only: return
        p_id = event.GetItem().GetData()
        self.current_selected_id = p_id
        dprint(f"on_fm_list_right_click {p_id}")
        item = event.GetItem()
        text = item.GetText()
        dprint(f"{text}")
        
        menu = wx.Menu()
        item_add_person = menu.Append(wx.ID_ANY, "Add Person")
        item_add_child = menu.Append(wx.ID_ANY, "Add Child")
        item_add_father = menu.Append(wx.ID_ANY, "Add Father")
        item_add_mother = menu.Append(wx.ID_ANY, "Add Mother")
        menu.AppendSeparator()
        item_add_husband = menu.Append(wx.ID_ANY, "Add Husband")
        item_add_wife = menu.Append(wx.ID_ANY, "Add Wife")
        menu.AppendSeparator()
        item_unlink_parents = menu.Append(wx.ID_ANY, "Unlink Parents")
        item_delete_person = menu.Append(wx.ID_ANY, "Delete This Person")
        item_delete_children = menu.Append(wx.ID_ANY, "Delete All Children")
        
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_person(), item_add_person)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_child(p_id), item_add_child)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_parent(p_id, "father"), item_add_father)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_parent(p_id, "mother"), item_add_mother)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_spouse(p_id, "husband"), item_add_husband)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_spouse(p_id, "wife"), item_add_wife)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_unlink_parents(p_id), item_unlink_parents)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_delete_person(p_id), item_delete_person)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_delete_children(p_id), item_delete_children)
        self.PopupMenu(menu)
        menu.Destroy()
        
    def on_fm_list_selection(self, event):
        p_id = event.GetItem().GetData()
        self.current_selected_id = p_id
        self.notebook.SetSelection(0)
        self.load_person_by_id(p_id)
        
    def on_export_vcard_ui(self, event):
        with wx.FileDialog(self, "Export to vCard", wildcard="vCard files (*.vcf)|*.vcf", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                count = self.export_vcard(dlg.GetPath())
                wx.MessageBox(f"Successfully exported {count} contacts to vCard.", "Export Complete", wx.ICON_INFORMATION)

    def on_import_vcard_ui(self, event):
        with wx.FileDialog(self, "Import from vCard", wildcard="vCard files (*.vcf)|*.vcf", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                count = self.import_vcard(dlg.GetPath())
                self.refresh_tree() 
                wx.MessageBox(f"Successfully imported {count} contacts from vCard.", "Import Complete", wx.ICON_INFORMATION)


    def on_list_right_click(self, event):
        """Routes right-click events from the left-panel list based on the active view mode."""
        # dprint("on_list_right_click: {event.GetItem().GetData()}")
        mode = self.view_mode.GetSelection()
        
        if mode == 1:  # People List Mode
            self.on_fm_list_right_click(event)
            
        elif mode == 2:  # Family List Mode
            self.on_family_list_right_click(event)

    def on_family_list_right_click(self, event):
        if self.db.read_only: return
        
        f_id = event.GetItem().GetData()
        self.current_selected_family_id = f_id
        
        # Fetch the family name and group for the menu display
        self.db.cursor.execute("SELECT family_group, family_name FROM families WHERE family_id = ?", (f_id,))
        res = self.db.cursor.fetchone()
        f_group = res[0] if res and res[0] else "Unknown"
        f_name = res[1] if res and res[1] else "Unknown"
        
        menu = wx.Menu()
        item_graph = menu.Append(wx.ID_ANY, f"Show Network Graph for '{f_group}'")
        item_graph_ppl = menu.Append(wx.ID_ANY, f"Show Graph (with People) for '{f_group}'")
        item_report_grp = menu.Append(wx.ID_ANY, f"Generate Report for Group '{f_group}'")
        item_report_fam = menu.Append(wx.ID_ANY, f"Generate Report for Branch '{f_name}'")        

        self.Bind(wx.EVT_MENU, lambda e: self.ctx_show_family_graph(f_group, include_people=False), item_graph)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_show_family_graph(f_group, include_people=True), item_graph_ppl)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_generate_full_report(target_group=f_group), item_report_grp)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_generate_full_report(target_group=f_group, target_family=f_name), item_report_fam)

        menu.AppendSeparator()
        item_exp_graphml = menu.Append(wx.ID_ANY, "Export in graphml")
        item_exp_gexf = menu.Append(wx.ID_ANY, "Export in gexf")
        item_exp_gml = menu.Append(wx.ID_ANY, "Export in gml")

        self.Bind(wx.EVT_MENU, lambda e: self.ctx_export_family_graph(f_group, 'graphml'), item_exp_graphml)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_export_family_graph(f_group, 'gexf'), item_exp_gexf)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_export_family_graph(f_group, 'gml'), item_exp_gml)
        
        menu.AppendSeparator()
        item_extract_sub_db = menu.Append(wx.ID_ANY, f"Extract '{f_name}' Segment to Standalone File...")
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_extract_segment_ui(target_group=f_group, target_family=f_name), item_extract_sub_db)

        if not self.db.read_only:
            
            menu.AppendSeparator()
            item_add = menu.Append(wx.ID_ANY, "Add New Family Group")
            item_add_sub = menu.Append(wx.ID_ANY, f"Add Subfamily to '{f_group}'")
            menu.AppendSeparator()
            item_rename = menu.Append(wx.ID_ANY, f"Rename '{f_name}'")
            item_move = menu.Append(wx.ID_ANY, f"Move '{f_name}' to another Group")
            menu.AppendSeparator()
            item_delete = menu.Append(wx.ID_ANY, f"Delete '{f_name}' Family")
        
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_new_family(), item_add)
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_subfamily_list(f_group), item_add_sub)
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_rename_folder_list(f_name), item_rename)
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_move_folder_list(f_name), item_move)
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_delete_family(f_id, f_name), item_delete)
        
        self.PopupMenu(menu)
        menu.Destroy()

    def ctx_show_family_graph(self, family_group, include_people=False):
        try:
            import networkx as nx
            import matplotlib.pyplot as plt
        except ImportError:
            wx.MessageBox("Please install networkx and matplotlib to use this feature.\n\nCommand: pip install networkx matplotlib", "Missing Libraries", wx.ICON_ERROR)
            return

        self.db.cursor.execute("SELECT family_name, ancestral_family_name FROM families WHERE family_group = ?", (family_group,))
        records = self.db.cursor.fetchall()
        
        if not records:
            wx.MessageBox(f"No family branches found for group '{family_group}'.", "Empty Group", wx.ICON_INFORMATION)
            return

        G = nx.DiGraph()
        
        for family_name, ancestral_name in records:
            parent = ancestral_name if ancestral_name and ancestral_name.strip() else family_group
            if parent != family_name:
                G.add_edge(parent, family_name)
            else:
                G.add_node(family_name)

        # 1. DYNAMIC SPACING: Pre-scan schema and records to find the absolute longest list of people
        max_people = 0
        if include_people:
            self.db.cursor.execute("PRAGMA table_info(people)")
            p_cols = [row[1].lower() for row in self.db.cursor.fetchall()]
            fam_col = next((c for c in p_cols if c == 'family_name'), 'family_name')
            anc_col = next((c for c in p_cols if 'ancestral' in c), None)

            for node in G.nodes():
                try:
                    if anc_col:
                        query = f"SELECT COUNT(*) FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?)) OR LOWER(TRIM({anc_col})) = LOWER(TRIM(?))"
                        self.db.cursor.execute(query, (node, node))
                    else:
                        query = f"SELECT COUNT(*) FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?))"
                        self.db.cursor.execute(query, (node,))
                    count = self.db.cursor.fetchone()[0]
                    if count > max_people: 
                        max_people = count
                except Exception:
                    pass
                    
        vertical_gap = max(2.5, (max_people * 0.4) + 1.5) if include_people else 2.0
        horizontal_gap = 5.0 if include_people else 3.5

        # True Tree Layout Calculation
        roots = [n for n, d in G.in_degree() if d == 0]
        
        for n in G.nodes():
            paths = [nx.shortest_path_length(G, r, n) for r in roots if nx.has_path(G, r, n)]
            G.nodes[n]['layer'] = min(paths) if paths else 0
            
        pos = {}
        current_y = [0.0]
        visited = set()

        def place_node(node):
            if node in visited: return pos[node][1]
            visited.add(node)
            
            children = list(G.successors(node))
            if not children:
                y = current_y[0]
                current_y[0] -= vertical_gap  
            else:
                child_ys = [place_node(c) for c in children]
                y = sum(child_ys) / len(child_ys)
                
            pos[node] = (G.nodes[node]['layer'] * horizontal_gap, y) 
            return y

        for root in sorted(roots):
            place_node(root)
            
        for node in G.nodes():
            if node not in visited:
                place_node(node)

        # 2. DYNAMIC FIGURE SIZING
        y_vals = [p[1] for p in pos.values()]
        x_vals = [p[0] for p in pos.values()]
        
        y_range = abs(max(y_vals) - min(y_vals)) if y_vals else 10
        x_range = abs(max(x_vals) - min(x_vals)) if x_vals else 10
        
        ideal_w = max(12.0, (x_range * 0.5) + 4.0)
        ideal_h = max(9.0, (y_range * 0.4) + 4.0)
        
        fig_w = min(ideal_w, 18.0)
        fig_h = min(ideal_h, 10.0)

        plt.figure(figsize=(fig_w, fig_h))
        title_suffix = " (with People)" if include_people else ""
        plt.title(f"Lineage Network: {family_group} Family Group{title_suffix}", fontsize=14, fontweight='bold')
        
        node_colors = []
        for node in G.nodes():
            if node == family_group or G.in_degree(node) == 0:
                node_colors.append('#2ecc71') 
            elif G.out_degree(node) == 0:
                node_colors.append('#e74c3c') 
            else:
                node_colors.append('#3498db') 
                
        edges = G.edges()
        edge_colors = range(len(edges)) 
        
        nodes_draw = nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=2500, edgecolors='black', linewidths=1.5)
        if nodes_draw: nodes_draw.set_zorder(1)
            
        edges_draw = nx.draw_networkx_edges(G, pos, edgelist=edges, edge_color=edge_colors, edge_cmap=plt.cm.viridis, width=2, alpha=0.7, arrowsize=20)
        if edges_draw:
            if isinstance(edges_draw, list):
                for patch in edges_draw: patch.set_zorder(2)
            else:
                edges_draw.set_zorder(2)
                
        labels_draw = nx.draw_networkx_labels(
            G, pos, font_size=9, font_weight='bold',
            bbox=dict(facecolor='white', edgecolor='black', boxstyle='round,pad=0.3', alpha=0.85)
        )
        if labels_draw:
            for _, text_obj in labels_draw.items():
                text_obj.set_zorder(3)

        # 3. RENDER PEOPLE LISTS (Dual-Column Matching)
        if include_people:
            self.db.cursor.execute("PRAGMA table_info(people)")
            columns = [row[1].lower() for row in self.db.cursor.fetchall()]
            
            fam_col = next((c for c in columns if c == 'family_name'), 'family_name')
            anc_col = next((c for c in columns if 'ancestral' in c), None)
            
            name_col = next((i for i, c in enumerate(columns) if 'name' in c and 'family' not in c), 0)
            fam_idx = columns.index(fam_col) if fam_col in columns else None
            moved_col = next((i for i, c in enumerate(columns) if 'moved' in c), None)
            departed_col = next((i for i, c in enumerate(columns) if 'depart' in c or 'deceas' in c or 'dead' in c), None)
            status_col = next((i for i, c in enumerate(columns) if c == 'status'), None)

            for node in G.nodes():
                try:
                    if anc_col:
                        query = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?)) OR LOWER(TRIM({anc_col})) = LOWER(TRIM(?))"
                        self.db.cursor.execute(query, (node, node))
                    else:
                        query = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?))"
                        self.db.cursor.execute(query, (node,))
                        
                    fuzzy_matches = self.db.cursor.fetchall()
                    
                    if not fuzzy_matches:
                        continue
                        
                    x, y = pos[node]
                    text_y = y - 0.45  
                    
                    for row in fuzzy_matches:
                        raw_name = row[name_col]
                        p_name = f"<Blank Name, ID: {row[0]}>" if not raw_name or not str(raw_name).strip() else str(raw_name).strip()
                            
                        p_color = 'black' 
                        is_departed = False
                        is_moved = False

                        if departed_col is not None:
                            if str(row[departed_col]).strip().lower() in ['1', 'true', 'yes', 'y']: is_departed = True
                            
                        if moved_col is not None:
                            if str(row[moved_col]).strip().lower() in ['1', 'true', 'yes', 'y']: is_moved = True

                        if status_col is not None:
                            val = str(row[status_col]).lower()
                            if 'departed' in val or 'deceased' in val or 'dead' in val: is_departed = True
                            elif 'moved' in val: is_moved = True

                        # DYNAMIC MOVED DETECTION: If they matched via ancestry, but their current family isn't this node, they left.
                        if fam_idx is not None:
                            current_fam = str(row[fam_idx]).strip().lower() if row[fam_idx] else ""
                            node_lower = str(node).strip().lower()
                            if current_fam != node_lower:
                                is_moved = True

                        if is_departed: p_color = 'red'
                        elif is_moved: p_color = 'gray'
                            
                        txt = plt.text(x, text_y, p_name, color=p_color, fontsize=9, fontweight='bold', 
                                       ha='center', va='top', zorder=10)
                        txt.set_bbox(dict(facecolor='#f8f9fa', edgecolor='lightgray', alpha=0.95, boxstyle='round,pad=0.2'))
                        
                        text_y -= 0.35 
                        
                except Exception as e:
                    wx.MessageBox(f"CRITICAL RENDER ERROR on node '{node}':\n{str(e)}", "Graph Rendering Error", wx.ICON_ERROR)
            
        if y_vals and x_vals:
            bottom_padding = (max_people * 0.4) + 2.0 if include_people else 2.0
            plt.ylim(min(y_vals) - bottom_padding, max(y_vals) + 2)
            plt.xlim(min(x_vals) - 2, max(x_vals) + horizontal_gap + 1.0)

        plt.axis('off')
        plt.tight_layout()
        plt.show()

    def ctx_export_family_graph(self, family_group, file_format):
        try:
            import networkx as nx
        except ImportError:
            wx.MessageBox("Please install networkx to use this feature.\n\nCommand: pip install networkx", "Missing Libraries", wx.ICON_ERROR)
            return

        self.db.cursor.execute("SELECT family_name, ancestral_family_name FROM families WHERE family_group = ?", (family_group,))
        records = list(set(self.db.cursor.fetchall()))

        if not records:
            wx.MessageBox(f"No family branches found for group '{family_group}'.", "Empty Group", wx.ICON_INFORMATION)
            return

        G = nx.DiGraph()
        
        # 1. Build the Family Nodes
        for f_name, a_name in records:
            parent = a_name if a_name and a_name.strip() else family_group
            if parent != f_name:
                G.add_edge(parent, f_name)
                G.nodes[parent]['node_type'] = 'family'
                G.nodes[f_name]['node_type'] = 'family'
            else:
                G.add_node(f_name, node_type='family')

        # 2. Ask user if they want to inject the people data
        include_ppl = wx.MessageBox(
            "Include individual people and their personal data in the exported graph?", 
            "Include People?", 
            wx.YES_NO | wx.ICON_QUESTION
        ) == wx.YES

        # 3. Fetch and attach people
        if include_ppl:
            self.db.cursor.execute("PRAGMA table_info(people)")
            columns = [row[1].lower() for row in self.db.cursor.fetchall()]
            fam_col = next((c for c in columns if c == 'family_name'), 'family_name')
            anc_col = next((c for c in columns if 'ancestral' in c), None)

            family_nodes = list(G.nodes())
            for node in family_nodes:
                if anc_col:
                    q = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?)) OR LOWER(TRIM({anc_col})) = LOWER(TRIM(?))"
                    self.db.cursor.execute(q, (node, node))
                else:
                    q = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?))"
                    self.db.cursor.execute(q, (node,))
                    
                people_rows = self.db.cursor.fetchall()
                
                for row in people_rows:
                    row_dict = dict(zip(columns, row))
                    p_id = str(row_dict.get(columns[0], '0'))
                    p_name = str(row_dict.get('name', f'Unknown_{p_id}'))
                    
                    # Create a unique node ID to prevent overlapping with Family names
                    node_id = f"P_{p_id}_{re.sub(r'[^a-zA-Z0-9]', '_', p_name)}"
                    
                    # Sanitize attributes (networkx graphml exporter crashes on None types or dicts)
                    clean_attrs = {'node_type': 'person', 'label': p_name}
                    for k, v in row_dict.items():
                        if v is not None and str(v).strip() != "":
                            clean_attrs[k] = str(v)
                            
                    G.add_node(node_id, **clean_attrs)
                    G.add_edge(node, node_id, edge_type='member')

        wildcard = f"{file_format.upper()} files (*.{file_format})|*.{file_format}"
        safe_group = re.sub(r'[^a-zA-Z0-9]', '_', family_group)
        suffix = "_with_people" if include_ppl else ""
        default_file = f"{safe_group}_network{suffix}.{file_format}"

        with wx.FileDialog(self, f"Export to {file_format.upper()}", wildcard=wildcard, defaultFile=default_file, style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                path = dlg.GetPath()
                try:
                    if file_format == 'graphml':
                        nx.write_graphml(G, path)
                    elif file_format == 'gexf':
                        nx.write_gexf(G, path)
                    elif file_format == 'gml':
                        nx.write_gml(G, path)
                    wx.LogStatus(f"Graph successfully exported to {path}")
                except Exception as e:
                    wx.MessageBox(f"Failed to export graph: {e}", "Export Error", wx.ICON_ERROR)

        
    
    def ctx_add_subfamily_list(self, family_group):
        if self.db.read_only: return
        dlg = wx.TextEntryDialog(self, f"Enter new unique nickname for '{family_group}':", "Add Subfamily")
        if dlg.ShowModal() == wx.ID_OK:
            new_nick = dlg.GetValue().strip()
            if new_nick:
                try:
                    self.db.cursor.execute("INSERT INTO families (family_group, family_name) VALUES (?, ?)", 
                                           (family_group, new_nick))
                    self.db.conn.commit()
                    self.refresh_fm_list_view()
                    self.refresh_tree()
                except Exception as e:
                    wx.MessageBox(f"Error adding subfamily: {e}", "Database Error", wx.ICON_ERROR)
        dlg.Destroy()

    def ctx_rename_folder_list(self, old_name):
        if self.db.read_only: return
        dlg = wx.TextEntryDialog(self, f"Enter new name to replace '{old_name}':", "Rename", old_name)
        if dlg.ShowModal() == wx.ID_OK:
            new_name = dlg.GetValue().strip()
            if new_name and new_name != old_name:
                self.db.cursor.execute("UPDATE people SET family_name = ? WHERE family_name = ?", (new_name, old_name))
                self.db.cursor.execute("UPDATE families SET family_name = ? WHERE family_name = ?", (new_name, old_name))
                self.db.conn.commit()
                self.refresh_fm_list_view()
                self.refresh_tree()
        dlg.Destroy()

    def ctx_move_folder_list(self, old_nick):
        if self.db.read_only: return
        self.db.cursor.execute("SELECT DISTINCT family_group FROM families WHERE family_group IS NOT NULL AND family_group != ''")
        choices = [r[0] for r in self.db.cursor.fetchall()]
        
        dlg = wx.SingleChoiceDialog(self, f"Select target Family Group for '{old_nick}':", "Move Branch", choices)
        if dlg.ShowModal() == wx.ID_OK:
            target_family = dlg.GetStringSelection()
            self.db.cursor.execute("UPDATE people SET family_group = ? WHERE family_name = ?", (target_family, old_nick))
            self.db.cursor.execute("UPDATE families SET family_group = ? WHERE family_name = ?", (target_family, old_nick))
            self.db.conn.commit()
            self.refresh_fm_list_view()
            self.refresh_tree()
        dlg.Destroy()

    def ctx_add_new_family(self):
        if self.db.read_only: return
        dlg = wx.TextEntryDialog(self, "Enter new family name:", "Add Family")
        if dlg.ShowModal() == wx.ID_OK:
            new_name = dlg.GetValue().strip()
            if new_name:
                try:
                    # We initialize both group and name as the new name
                    self.db.cursor.execute("INSERT INTO families (family_group, family_name) VALUES (?, ?)", (new_name, new_name))
                    self.db.conn.commit()
                    self.refresh_fm_list_view()
                    self.refresh_tree()
                except Exception as e:
                    wx.MessageBox(f"Error adding family: {e}", "Database Error", wx.ICON_ERROR)
        dlg.Destroy()
        
    def ctx_delete_family(self, f_id, f_name):
        if self.db.read_only: return
        
        # Safety check: see if people are currently linked to this family
        self.db.cursor.execute("SELECT COUNT(*) FROM people WHERE family_name = ?", (f_name,))
        count = self.db.cursor.fetchone()[0]
        
        msg = f"Are you sure you want to delete the '{f_name}' family record?"
        if count > 0:
            msg += f"\n\nWARNING: There are {count} people associated with this family name. They will not be deleted, but their family link will be orphaned."
            
        if wx.MessageBox(msg, "Confirm Deletion", wx.YES_NO | wx.ICON_WARNING) == wx.YES:
            try:
                self.db.cursor.execute("DELETE FROM families WHERE family_id = ?", (f_id,))
                self.db.conn.commit()
                self.current_selected_family_id = None
                
                # Clear the family detail fields in the UI
                self.fam_txt_id.SetValue("")
                self.fam_txt_name.SetValue("")
                self.fam_txt_group.SetValue("")
                self.fam_txt_aname.SetValue("")
                self.fam_txt_notes.SetValue("")
                
                self.refresh_fm_list_view()
                self.refresh_tree()
            except Exception as e:
                wx.MessageBox(f"Error deleting family: {e}", "Database Error", wx.ICON_ERROR)

    from report_generator import ctx_generate_full_report
    
        
    def refresh_fm_list_view(self):
        dprint("refresh_fm_list_view")
        self.list_view.DeleteAllColumns()
        headers = ["Family ID", "Family Group", "Family Name"]
        widths = [70, 150, 150]
        for i, (h, w) in enumerate(zip(headers, widths)):
            if i == self.list_sort_col: h += " ▼" if self.list_sort_desc else " ▲"
            self.list_view.InsertColumn(i, h, width=w)

        self.db.cursor.execute("SELECT family_id, family_group, family_name FROM families ORDER BY family_group ASC") 
        self.current_list_data = self.db.cursor.fetchall()
        self.render_list_view()

    def render_list_view(self):
        self.list_view.DeleteAllItems()
        if not self.current_list_data: return
        
        query = self.main_search.GetValue().lower()
        
        filtered_data = []
        for row in self.current_list_data:
            if not query or any(query in str(item).lower() for item in row if item is not None):
                filtered_data.append(row)
                
        if self.list_sort_col < self.list_view.GetColumnCount():
            def sort_key(row):
                val = str(row[self.list_sort_col]).strip()
                try: return float(val)
                except ValueError: return val.lower()
            try: filtered_data.sort(key=sort_key, reverse=self.list_sort_desc)
            except IndexError: pass
        
        mode = self.view_mode.GetSelection()
        for idx, row in enumerate(filtered_data):
            if mode == 1:
                self.list_view.InsertItem(idx, str(row[0] if row[0] is not None else ""))
                self.list_view.SetItem(idx, 1, str(row[1] if row[1] is not None else ""))
                self.list_view.SetItem(idx, 2, str(row[2] if row[2] is not None else ""))
                self.list_view.SetItem(idx, 3, str(row[3] if row[3] is not None else ""))
                self.list_view.SetItem(idx, 4, str(row[4] if row[4] is not None else ""))
                self.list_view.SetItemData(idx, row[4])
            elif mode == 2:
                self.list_view.InsertItem(idx, str(row[0] if row[0] is not None else ""))
                self.list_view.SetItem(idx, 1, str(row[1] if row[1] is not None else ""))
                self.list_view.SetItem(idx, 2, str(row[2] if row[2] is not None else ""))
                self.list_view.SetItemData(idx, row[0])

    def on_main_search(self, event):
        self.render_list_view()

    def on_list_col_click(self, event):
        col = event.GetColumn()
        if col == self.list_sort_col:
            self.list_sort_desc = not self.list_sort_desc
        else:
            self.list_sort_col = col
            self.list_sort_desc = False
        
        for i in range(self.list_view.GetColumnCount()):
            col_info = self.list_view.GetColumn(i)
            text = col_info.GetText().replace(" ▲", "").replace(" ▼", "")
            if i == self.list_sort_col:
                text += " ▼" if self.list_sort_desc else " ▲"
            col_info.SetText(text)
            self.list_view.SetColumn(i, col_info)
            
        self.render_list_view()
        
    def on_begin_drag(self, event):
        if self.db.read_only:
            event.Veto()
            return
        self.dragged_item = event.GetItem()
        if self.dragged_item == self.root_item:
            event.Veto()
            return
        event.Allow()

    def on_end_drag(self, event):
        """Processes branch reallocations execution passes."""
        if self.db.read_only: return
        target_item = event.GetItem()
        if not self.dragged_item or not target_item or self.dragged_item == target_item:
            return

        dragged_person_id = self.tree.GetItemData(self.dragged_item)
        if not dragged_person_id:
            wx.MessageBox("You can only drag individual person records.", "Drag Denied", wx.ICON_WARNING)
            return

        target_text = self.tree.GetItemText(target_item)
        clean_family_group = target_text.split(" (")[0].replace(" ▾", "").replace(" ▸", "").strip()

        self.db.cursor.execute("SELECT family_name FROM people WHERE id = ?", (dragged_person_id,))
        dprint(f"target={target_text}") 
        confirm = wx.MessageBox(
            f"Move this individual directly into the '{clean_family_group}' folder branch?", 
            "Confirm Lineage Transfer", 
            wx.YES_NO | wx.ICON_QUESTION
        )
        if confirm == wx.YES:
            dprint(f"set {target_text} {dragged_person_id}")
            self.db.cursor.execute(
                "UPDATE people SET family_name = ? WHERE id = ?", 
                (clean_family_group, dragged_person_id)
            )
            self.db.conn.commit()
        self.refresh_tree()

    def on_refresh_tree(self, event=None):
        self.refresh_tree()

    def refresh_tree(self):
        dprint("refresh_tree: start")
        self.tree.DeleteAllItems()
        self.tree_item_table = {}
        self.tree_group_table = {}
        self.root_item = self.tree.AddRoot("All Families ▾")
        self.root_item.SetData("root_item")

        self.db.cursor.execute("SELECT family_group, family_name, ancestral_family_name FROM families")
        self.all_families = self.db.cursor.fetchall()
        dprint(f"refresh_tree: all_families= {self.all_families}")

        self.db.cursor.execute("SELECT name, family_name, ancestral_family_name FROM people")
        self.unique_groups = sorted(list(set(row[0] for row in self.all_families if row[0]))) 
        self.ans_table = {}
        for grp in self.unique_groups:
            self.ans_table[grp] = None

        for row in self.all_families:            
            self.ans_table[row[1]] = row[2] if row[2] else row[0]
            
        dprint(f"refresh_tree: unique groups = {self.unique_groups}")
        dprint(f"refresh_tree: ans_table = {self.ans_table}")
        
        class FamilyTable():
            def __init__(self):
                self.families = {}
                # self.groups = []
                self.orderd = []
                
            def add_family(self, f):
                # Added to fix missing nodes.
                # Check this
                if f[2] == '':
                    f_tmp = (f[0],f[1],f[0])
                    f = f_tmp
                if f[1] ==f[2] :
                    f_tmp = (f[0],f[1],f[0])
                    f = f_tmp
                # till here    
                if f[0] not in self.families.keys():
                    self.families[f[0]] = {}
                self.families[f[0]][f[1]] = list(f)
                
            def order(self):
                self.ordered = []
                alld = []
                for grp in self.families.keys():
                    self.orderd = []
                    fms = self.families[grp]               
                    for fm in fms:
                        self.orderd.append(fms[fm])
                        d = fms[fm]
                    alld.append(self.orderd)
            def add_group(self):
                # if the group is not in the families hash, add it.
                for grp in self.families.keys():
                    fms = self.families[grp]
                    if not any(row[1] == grp for row in fms):
                        self.families[grp][grp] = (grp,grp,'')                
            def groups(self):
                self.groups = {row[0] for row in self.all_families}
        ft = FamilyTable()
        for row in self.all_families:
            ft.add_family(row)
        # ft.add_group()  # add if group missing.
            
        ft.order()
        
        import networkx as nx
        
        for group in ft.families.keys():            
            graph_dict = ft.families[group]
            # graph_dict[group]=[group,group,''] # test
            G = nx.Graph()
            for source, values in graph_dict.items():
                neighbor = values[2]
                if source is not None and neighbor is not None:
                    G.add_edge(source, neighbor)
                elif source is not None:
                    G.add_node(source)
                elif neighbor is not None:
                    G.add_node(neighbor)

            from networkx.algorithms import dfs_predecessors
            x = dfs_predecessors(G)
            # breakpoint()
            
            item_hash = {}
            ans_node = self.add_tree_item(self.root_item, group)            
            item_hash[group] = ans_node
            # ans_node = self.add_tree_item(ans_node, group.lower())            
            # item_hash[group.lower()] = ans_node
            
            from networkx.algorithms import bfs_tree
            try:
                # x = bfs_tree(G, group.lower())
                # x = bfs_tree(G, group.lower())
                x = bfs_tree(G, group)
            except Exception as e:
                print(f"group error: {e}")
                continue
                
            fns_pairs = x.edges()
            # breakpoint()
            for fns_pair in fns_pairs:
                fn = fns_pair[0]
                if not ans_node:
                    if fn in item_hash:
                        ans_node = item_hash[fn]                    
                    else:
                        ans_node = self.root_item
                if fn not in item_hash:
                    ans_node = self.add_tree_item(ans_node, fn)
                    item_hash[fn] = ans_node
                fn = fns_pair[1]
                if fn not in item_hash:
                    ans_node = self.add_tree_item(ans_node, fn)
                    item_hash[fn] = ans_node
                ans_node = None
        return

    def add_tree_item(self, ans_node, fn):
        if fn == None or fn == '':
            return None
        fn_node = self.tree.AppendItem(ans_node, f"{fn} ▾")
        self.tree.SetItemData(fn_node, fn)
        return fn_node

    def find_group_fn(self, fn):
        for i, r in enumerate(self.all_families):
            if fn in r:
                return r
        return None

    def load_family(self, item):
        dprint(f"load_family: {item.GetData()}")
        parent = item.GetParent()
        fn = item.GetData()
        self.notebook.SetSelection(1)

        if not parent.GetData():
            self.db.cursor.execute("SELECT * FROM families WHERE family_group = ?", (fn,))
            res = self.db.cursor.fetchone()
            self.family_title_text.SetLabel(f"Family Group: {fn}")
            self.fam_txt_id.SetValue(str(res[0]) if res and res[0] else "")
            self.fam_txt_name.SetValue(res[1] if res and res[1] else "")
            self.fam_txt_group.SetValue(res[2] if res and res[2] else "")
            self.fam_txt_aname.SetValue(res[3] if res and res[3] else "")
            self.fam_txt_notes.SetValue(res[4] if res and res[4] else "")
            return
            
        subfam_key = fn
        self.current_selected_family_id = subfam_key
        self.db.cursor.execute("SELECT * FROM families WHERE family_name = ?", (subfam_key,))
        res = self.db.cursor.fetchone()
        if res is None:
            self.family_title_text.SetLabel(f"Family Group: {subfam_key}")
        else:
            self.family_title_text.SetLabel(f"Family: {subfam_key}")
            
        self.fam_txt_id.SetValue(str(res[0]) if res and res[0] else "")
        self.fam_txt_name.SetValue(res[1] if res and res[1] else "")
        self.fam_txt_group.SetValue(res[2] if res and res[2] else "")
        self.fam_txt_aname.SetValue(res[3] if res and res[3] else "")
        self.fam_txt_notes.SetValue(res[4] if res and res[4] else "")

    def on_save_family_details(self, event):
        """Updates the families table with current UI inputs."""
        if self.db.read_only: return
        if not self.current_selected_family_id:
            wx.MessageBox("No family selected to update.", "Selection Required", wx.ICON_WARNING)
            return

        name = self.fam_txt_name.GetValue().strip()
        group = self.fam_txt_group.GetValue().strip()
        aname = self.fam_txt_aname.GetValue().strip()        
        notes = self.fam_txt_notes.GetValue().strip()
        
        try:
            self.db.cursor.execute("""
                UPDATE families 
                SET family_name = ?,
                    family_group = ?,
                    ancestral_family_name = ?,
                    family_notes = ? 
                WHERE family_id = ?
            """, (name, group, aname, notes, self.current_selected_family_id))
            
            self.db.conn.commit()
            self.refresh_tree()
            wx.LogStatus(f"Family updated successfully.")
        except Exception as e:
            wx.MessageBox(f"Failed to update family details: {e}", "Database Error", wx.ICON_ERROR)
    
    def populate_member_list(self, fn):
        """Populates the middle_panel with active and ancestral members."""
        dprint(f"populate_member_list {fn}")
        self.fm_list_view.DeleteAllItems()
    
        self.db.cursor.execute("SELECT family_name FROM families WHERE family_name = ?", (fn,))
        self.db.cursor.execute("SELECT id, name, family_name, ancestral_family_name, deceased FROM people WHERE family_name = ? or ancestral_family_name = ?", (fn, fn,))
        lst = self.db.cursor.fetchall()
        for idx, (p_id, name, family_name, ancestral_family_name, is_deceased) in enumerate(lst):
            item = self.fm_list_view.InsertItem(idx, name)
            if ancestral_family_name == fn and family_name != fn:
                self.fm_list_view.SetItemTextColour(item, wx.Colour(128, 128, 128))
            self.fm_list_view.SetItemData(item, p_id)
            if is_deceased:
                self.fm_list_view.SetItemTextColour(item, wx.Colour(139, 69, 19)) 
                
    def on_selection_changed(self, event):
        item = event.GetItem()
        if not item or not item.IsOk() or item == self.root_item: return

        f_id = self.tree.GetItemData(item)
        if item.GetParent().GetData() is not None:  
            self.populate_member_list(f_id)
        else:
            self.fm_list_view.DeleteAllItems()
                          
        self.load_family(item)
        self.notebook.SetSelection(1) 

    def resolve_relation_field(self, input_str):
        if self.db.read_only: return ""
        input_str = input_str.strip()
        if not input_str: return ""
        if "(" in input_str and ")" in input_str:
            try:
                possible_id = input_str.split("(")[-1].replace(")", "").strip()
                if possible_id.isdigit(): return str(possible_id)
            except: pass
        if input_str.isdigit(): return str(input_str)

        self.db.cursor.execute("SELECT id, name, family_group FROM people WHERE name = ?", (input_str,))
        matches = self.db.cursor.fetchall()
        if len(matches) == 1: return str(matches[0][0])
        if len(matches) > 1:
            choices = [f"{m[1]} {m[2] if m[2] else ''} (ID: {m[0]})" for m in matches]
            dlg = wx.SingleChoiceDialog(self, f"Select correctly:", "Conflict", choices)
            res = str(matches[dlg.GetSelection()][0]) if dlg.ShowModal() == wx.ID_OK else "CANCELLED"
            dlg.Destroy(); return res

        msg_box = wx.MessageDialog(self, f"Register '{input_str}'?", "New Row", wx.YES_NO | wx.CANCEL)
        msg_box.SetYesNoCancelLabels("Create Record", "Save as Text", "Cancel")
        choice = msg_box.ShowModal(); msg_box.Destroy()
        
        if choice == wx.ID_YES:
            self.db.cursor.execute("INSERT INTO people (name) VALUES (?)", (input_str,))
            self.db.conn.commit(); return str(self.db.cursor.lastrowid)
        elif choice == wx.ID_NO: return input_str 
        else: return "CANCELLED"

    def _display_relational_field(self, data_dict, field_key, UI_field_name):
        val = data_dict.get(field_key, "")
        if not val:
            self.fields[UI_field_name].SetValue("")
            return
        if str(val).isdigit():
            self.db.cursor.execute("SELECT name FROM people WHERE id = ?", (int(val),))
            p_name = self.db.cursor.fetchone()
            self.fields[UI_field_name].SetValue(f"{p_name[0]} ({val})" if p_name else f"Unknown (ID: {val})")
        else:
            self.fields[UI_field_name].SetValue(str(val))

    def _display_multi_relational_field(self, data_dict, field_key, UI_field_name):
        raw_val = data_dict.get(field_key, "")
        if not raw_val:
            self.fields[UI_field_name].SetValue("")
            return
            
        parts = [p.strip() for p in str(raw_val).split(',')]
        display_parts = []
        for p in parts:
            if p.isdigit():
                self.db.cursor.execute("SELECT name FROM people WHERE id = ?", (int(p),))
                p_name = self.db.cursor.fetchone()
                display_parts.append(f"{p_name[0]} ({p})" if p_name else f"Unknown (ID: {p})")
            elif p:
                display_parts.append(p)
                
        self.fields[UI_field_name].SetValue(", ".join(display_parts))

    def load_person_by_id(self, p_id):
        self.current_selected_id = p_id
        self.db.cursor.execute("SELECT * FROM people WHERE id=?", (p_id,))
        row = self.db.cursor.fetchone()
        
        if row:
            colnames = [d[0] for d in self.db.cursor.description]
            data = dict(zip(colnames, row))
            # self.name_display.SetLabel(data['name'])
            self.name_label.SetLabel(data['name'])
            for key in self.fields:
                if key not in ["father_name", "mother_name", "husband_names", "wife_names", "son_names", "daughter_names"]:
                    val = data.get(key, "")
                    self.fields[key].SetValue(str(val) if val is not None else "")
            
            self._display_relational_field(data, 'father_id', 'father_name')
            self._display_relational_field(data, 'mother_id', 'mother_name')
            self._display_multi_relational_field(data, 'husband_ids', 'husband_names')
            self._display_multi_relational_field(data, 'wife_ids', 'wife_names')
            self._display_multi_relational_field(data, 'son_ids', 'son_names')
            self._display_multi_relational_field(data, 'daughter_ids', 'daughter_names')
            
            self.load_and_display_photo(data.get('local_photo_path'))

            new_qr_bitmap = self.generate_vcard_qr(data['name'])
            self.qr_ctrl.SetBitmap(new_qr_bitmap)
            self.qr_ctrl.Refresh()
            self.tab_person.Layout() 
            

    def clean_id_list(self, raw_input):
        if not raw_input: return ""
        cleaned_parts = [part.strip() for part in str(raw_input).split(',') if part.strip()]
        seen = set()
        unique_parts = [x for x in cleaned_parts if not (x in seen or seen.add(x))]
        valid_ids = []
        for x in unique_parts:
            if "(" in x and ")" in x:
                try:
                    extracted = x.split("(")[-1].replace(")", "").strip()
                    if extracted.isdigit(): valid_ids.append(extracted)
                except: pass
            elif x.isdigit(): valid_ids.append(x)
        return ", ".join(valid_ids)

            
    def on_save_details(self, event):
        if self.db.read_only: return
        if not self.current_selected_id: return
        
        h_ids = self.clean_id_list(self.fields['husband_names'].GetValue())
        w_ids = self.clean_id_list(self.fields['wife_names'].GetValue())
        s_ids = self.clean_id_list(self.fields['son_names'].GetValue())
        d_ids = self.clean_id_list(self.fields['daughter_names'].GetValue())

        relation_map = {
            'father_id': self.resolve_relation_field(self.fields['father_name'].GetValue()),
            'mother_id': self.resolve_relation_field(self.fields['mother_name'].GetValue()),
            'husband_ids': h_ids,
            'wife_ids': w_ids,
            'son_ids': s_ids,
            'daughter_ids': d_ids
        }
        if "CANCELLED" in relation_map.values(): return

        self.db.cursor.execute("PRAGMA table_info(people)")
        db_columns = [row[1] for row in self.db.cursor.fetchall()]

        update_pairs = []
        bind_values = []
        for col in db_columns:
            if col == 'id': continue
            if col in relation_map:
                update_pairs.append(f"{col} = ?")
                bind_values.append(relation_map[col])
            elif col in self.fields:
                update_pairs.append(f"{col} = ?")
                bind_values.append(self.fields[col].GetValue().strip())

        sql_query = f"UPDATE people SET {', '.join(update_pairs)} WHERE id = ?"
        bind_values.append(self.current_selected_id)

        try:
            self.db.cursor.execute(sql_query, bind_values)
            self.db.conn.commit()
            self.refresh_tree()
            wx.LogStatus("Record and family branches updated successfully.")
        except Exception as e:
            wx.MessageBox(f"Update failed: {e}", "Database Error", wx.ICON_ERROR)

        mode = self.view_mode.GetSelection()
        if mode == 0:   
            self.refresh_tree()
        elif mode == 1: 
            self.refresh_list_view()
        elif mode == 2: 
            self.refresh_fm_list_view()
        wx.LogStatus("Record updated successfully.")



        
    def update_descendant_family(self, person_id, new_family):
        if self.db.read_only: return
        self.db.cursor.execute("UPDATE people SET family_group = ? WHERE id = ?", (new_family, person_id))
        for c_id, _ in self.db.get_children(person_id):
            self.update_descendant_family(c_id, new_family)
        
    def on_add_person_action(self, event):
        """Adds a new person record contextually."""
        if self.db.read_only: return
        sel = None
        if self.tree.IsShown():
            sel = self.tree.GetSelection()

        family_name = ""
        p_id = None
    
        if sel and sel.IsOk():
            if self.tree.IsShown():
                data = self.tree.GetItemData(sel)
                if data and not isinstance(data, str): 
                    p_id = data
                    self.db.cursor.execute("SELECT family_name FROM people WHERE id=?", (p_id,))
                    res = self.db.cursor.fetchone()
                    if res: family_name = res[0]
                else: 
                    family_name = self.tree.GetItemText(sel).split(" (")[0].replace(" ▾", "").replace(" ▸", "").strip()

        self.db.cursor.execute("INSERT INTO people (name, family_name, father_id) VALUES (?, ?, ?)", ("New Person", family_name, p_id))
        self.db.conn.commit()
        new_id = self.db.cursor.lastrowid
    
        self.refresh_tree()
        
        if self.tree.IsShown():
            item = self.find_item_by_id(self.root_item, new_id)
            if item: 
                self.tree.SelectItem(item)
                self.load_person_by_id(new_id)
                self.fields['name'].SetFocus()

    def find_item_by_id(self, parent, target_id):
        if not parent or not parent.IsOk(): return None
        item, cookie = self.tree.GetFirstChild(parent)
        if not item: return None
        while item and item.IsOk():
            if self.tree.GetItemData(item) == target_id: 
                if "Head ID:" not in self.tree.GetItemText(item):
                    return item
            found = self.find_item_by_id(item, target_id)
            if found: return found
            item, cookie = self.tree.GetNextChild(parent, cookie)
        return None

    def on_change_photo(self, event):
        if self.db.read_only: return
        if not self.current_selected_id: return
        dlg = wx.FileDialog(self, "Choose Photo", wildcard="Images|*.jpg;*.png;*.bmp", style=wx.FD_OPEN)
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            self.fields['local_photo_path'].SetValue(path)
            self.load_and_display_photo(path)
        dlg.Destroy()

    def load_and_display_photo(self, path):
        if path and os.path.exists(path):
            img = wx.Image(path, wx.BITMAP_TYPE_ANY)
            img = img.Scale(120, 150, wx.IMAGE_QUALITY_HIGH)
            self.photo_ctrl.SetBitmap(wx.Bitmap(img))
        else: self.photo_ctrl.SetBitmap(self.default_bmp)

    def on_add_custom_field(self, event):
        if self.db.read_only: return
        with AddFieldDialog(self) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                label = dlg.name_ctrl.GetValue().strip()
                if not label: return
                db_col = label.lower().replace(" ", "_")
                is_multiline = dlg.multiline_cb.IsChecked()
                try:
                    self.db.cursor.execute(f"ALTER TABLE people ADD COLUMN {db_col} TEXT")
                    self.db.conn.commit()
                    self.add_single_field_to_ui(db_col, label, is_multiline)
                    self.tab_person.Layout()
                except Exception as e: wx.MessageBox(f"Field error: {e}", "Error", wx.ICON_ERROR)
        
    def add_single_field_to_ui(self, key, label, multiline):
        lbl = wx.StaticText(self.tab_person, label=label)
        
        # Don't apply PROCESS_ENTER to multiline fields
        style = wx.TE_MULTILINE if multiline else wx.TE_PROCESS_ENTER
        if self.db.read_only:
            style |= wx.TE_READONLY
            
        txt = wx.TextCtrl(self.tab_person, style=style)
        
        if not self.db.read_only and not multiline:
            txt.Bind(wx.EVT_TEXT_ENTER, self.on_save_details)
            
        if multiline:
            txt.SetMinSize((-1, 80))
            self.details_grid.Add(lbl, 0, wx.ALIGN_TOP | wx.TOP, 5)
        else: 
            self.details_grid.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
            
        self.details_grid.Add(txt, 1, wx.EXPAND | wx.ALL, 5)
        self.fields[key] = txt
        
    def on_export_csv(self, event):
        with wx.FileDialog(self, "Export to CSV", wildcard="CSV files (*.csv)|*.csv", style=wx.FD_SAVE) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                success, msg = self.io_helper.export_to_csv(dlg.GetPath())
                wx.MessageBox(msg, "Export Result" if success else "Error")

    def on_import_csv(self, event):
        if self.db.read_only: return
        with wx.FileDialog(self, "Select People CSV", wildcard="CSV files (*.csv)|*.csv", style=wx.FD_OPEN) as dlg1:
            if dlg1.ShowModal() != wx.ID_OK: return
            people_path = dlg1.GetPath()
            
        with wx.FileDialog(self, "Select Families CSV", wildcard="CSV files (*.csv)|*.csv", style=wx.FD_OPEN) as dlg2:
            if dlg2.ShowModal() != wx.ID_OK: return
            families_path = dlg2.GetPath()
            
        success, msg = self.io_helper.import_from_csv(people_path, families_path)
        if success: self.refresh_tree()
        wx.MessageBox(msg, "CSV Import")

    def on_import_gedcom(self, event):
        if self.db.read_only: return
        with wx.FileDialog(self, "Open GEDCOM file", wildcard="GEDCOM files (*.ged)|*.ged", style=wx.FD_OPEN) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                success, msg = self.io_helper.import_from_gedcom(dlg.GetPath())
                if success: self.refresh_tree()
                wx.MessageBox(msg, "GEDCOM Import")

    def on_export_gedcom(self, event):
        with wx.FileDialog(self, "Export to GEDCOM", wildcard="GEDCOM files (*.ged)|*.ged", style=wx.FD_SAVE) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                success, msg = self.io_helper.export_to_gedcom(dlg.GetPath())
                wx.MessageBox(msg, "GEDCOM Export Result" if success else "Error")

    def on_export_json(self, event):
        with wx.FileDialog(self, "Export to JSON", wildcard="JSON files (*.json)|*.json", style=wx.FD_SAVE) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                success, msg = self.io_helper.export_to_json(dlg.GetPath())
                wx.MessageBox(msg, "JSON Export Result" if success else "Error")
                
    def on_import_json(self, event):
        if self.db.read_only: return
        with wx.FileDialog(self, "Select JSON File", wildcard="JSON files (*.json)|*.json", style=wx.FD_OPEN) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                success, msg = self.io_helper.import_from_json(dlg.GetPath())
                if success: self.refresh_tree()
                wx.MessageBox(msg, "JSON Import")
                
    def on_remove_duplicates(self, event):
        if self.db.read_only: return
        if wx.MessageBox("Merge records with identical Names?", "Cleanup", wx.YES_NO) == wx.YES:
            success, msg = self.db.remove_duplicates()
            if success: self.refresh_tree(); wx.MessageBox(msg, "Success")

    def on_toggle_view(self, event):
        mode = self.view_mode.GetSelection()
        self.tree.Hide()
        self.list_view.Hide()
        self.main_search.Hide()
        
        if mode == 0:   
            self.tree.Show()
            self.refresh_tree()
        elif mode == 1: 
            self.list_sort_col = 0
            self.list_sort_desc = False
            self.list_view.Show()
            self.main_search.Show()
            self.refresh_list_view()
        elif mode == 2: 
            self.list_sort_col = 1
            self.list_sort_desc = False
            self.list_view.Show()
            self.main_search.Show()
            self.refresh_fm_list_view()
        self.left_panel.Layout()

        
    def refresh_list_view(self):
        self.list_view.DeleteAllColumns()
        headers = ["Name", "Family", "Nickname", "Family ID", "ID"]
        widths = [150, 100, 100, 70, 40]
        for i, (h, w) in enumerate(zip(headers, widths)):
            if i == self.list_sort_col: h += " ▼" if self.list_sort_desc else " ▲"
            self.list_view.InsertColumn(i, h, width=w)

        self.db.cursor.execute("SELECT name, family_group, family_name, family_id, id FROM people ORDER BY name ASC")
        self.current_list_data = self.db.cursor.fetchall()
        self.render_list_view()

    def on_list_selection(self, event):
        mode = self.view_mode.GetSelection()
        
        if mode == 1: # People List Mode
            self.current_selected_family_id = None
            self.notebook.SetSelection(0)
            self.load_person_by_id(event.GetData())
            
        elif mode == 2: # Family List Mode
            f_id = event.GetData()
            self.current_selected_family_id = f_id
            self.notebook.SetSelection(1)
            
            # Explicitly select columns to guarantee correct assignment order
            self.db.cursor.execute("SELECT family_id, family_name, family_group, ancestral_family_name, family_notes FROM families WHERE family_id = ?", (f_id,))
            res = self.db.cursor.fetchone()
            
            if res:
                fam_id, fam_name, fam_group, anc_name, fam_notes = res
                
                self.family_title_text.SetLabel(f"Family Profile: {fam_name}") 
                self.fam_txt_id.SetValue(str(fam_id) if fam_id else "")
                self.fam_txt_name.SetValue(fam_name if fam_name else "") 
                self.fam_txt_group.SetValue(fam_group if fam_group else "")
                self.fam_txt_aname.SetValue(anc_name if anc_name else "")
                self.fam_txt_notes.SetValue(fam_notes if fam_notes else "")
                
                # --- v19.34 THE FIX: Update the middle panel list with family members ---
                if fam_name:
                    self.populate_member_list(fam_name)

    def on_delete_action(self, event):
        ### IMPORTANT
        dprint("on_delete_action: disabled for now")
        return
    
        if self.db.read_only: return
        if not self.current_selected_id:
            wx.MessageBox("Please select a person from the list or tree first.", "No Selection", wx.ICON_INFORMATION)
            return
            
        self.db.cursor.execute("SELECT name FROM people WHERE id=?", (self.current_selected_id,))
        res = self.db.cursor.fetchone()
        name = res[0] if res else "Unknown Person"
        
        msg = f"Are you sure you want to delete {name} (ID: {self.current_selected_id})?\n\n" \
              "Delete Individual - Keeps children in database (unlinked).\n" \
              "Delete Entire Branch - Permanently purges this person AND all descendants."
              
        dlg = wx.MessageDialog(self, msg, "Confirm Deletion", wx.YES_NO | wx.CANCEL | wx.ICON_WARNING)
        dlg.SetYesNoCancelLabels("Delete Individual", "Delete Entire Branch", "Cancel")
        result = dlg.ShowModal()
        dlg.Destroy()
        
        if result == wx.ID_YES:
            success, msg = self.db.delete_person(self.current_selected_id)
            if success:
                self.current_selected_id = None
                # self.name_display.SetLabel("Select a Person")
                self.name_label.SetLabel("Select a Person")
                for key in self.fields: self.fields[key].SetValue("")
                self.load_and_display_photo(None)
                mode = self.view_mode.GetSelection()
                if mode == 0:    
                    self.refresh_tree()
                elif mode == 1:  
                    self.refresh_list_view()
                elif mode == 2:  
                    self.refresh_fm_list_view()
                wx.MessageBox(msg, "Deleted", wx.ICON_INFORMATION)
                
        elif result == wx.ID_NO:
            if wx.MessageBox(f"CRITICAL: This will wipe out all descendants of {name}. Proceed?", "Confirm Cascade Delete", wx.YES_NO | wx.ICON_ERROR) == wx.YES:
                if self.db.delete_branch_recursive(self.current_selected_id):
                    self.db.conn.commit()
                    self.current_selected_id = None
                    # self.name_display.SetLabel("Select a Person")
                    self.name_label.SetLabel("Select a Person")
                    for key in self.fields: self.fields[key].SetValue("")
                    self.load_and_display_photo(None)
                    mode = self.view_mode.GetSelection()
                    if mode == 0:    
                        self.refresh_tree()
                    elif mode == 1:  
                        self.refresh_list_view()
                    elif mode == 2:  
                        self.refresh_fm_list_view()
                    wx.MessageBox("Branch deleted successfully.", "Branch Purged", wx.ICON_INFORMATION)
        
    def on_exit_app(self, event): self.Close(True)

    def on_tree_right_click(self, event):
        dprint(f"on_tree_right_click: {event.GetItem().GetData()}")
        item = event.GetItem()
        dprint(f"root_item= {item.GetData()} : {self.root_item} item: {item}")
        if not item.IsOk(): return # or item == self.root_item: return
        self.tree.SelectItem(item)

        if item == self.root_item: #  or not self.tree.GetItemParent(item).IsOk():
            dprint("on self.root_item right_click")
            menu = wx.Menu()
            item_report = menu.Append(wx.ID_ANY, "Generate Full Family Tree Report (HTML & LaTeX)")
            
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_generate_full_report(), item_report)
            
            self.PopupMenu(menu)
            menu.Destroy()
            return
        
        node_name = self.tree.GetItemData(item)
        if not node_name:
            dprint(f"No family selected for report generation{node_name}")
            return
        
        self.db.cursor.execute("SELECT family_group FROM families WHERE family_name = ?", (node_name,))
        res = self.db.cursor.fetchone()
        f_group = res[0] if res and res[0] else node_name
        
        menu = wx.Menu()        
        item_graph = menu.Append(wx.ID_ANY, f"Show Network Graph for '{f_group}'")
        item_graph_ppl = menu.Append(wx.ID_ANY, f"Show Graph (with People) for '{f_group}'")
        item_report_grp = menu.Append(wx.ID_ANY, f"Generate Report for Group '{f_group}'")
        item_report_fam = menu.Append(wx.ID_ANY, f"Generate Report for Branch '{node_name}'")     
        menu.AppendSeparator()

        item_exp_graphml = menu.Append(wx.ID_ANY, "Export in graphml")
        item_exp_gexf = menu.Append(wx.ID_ANY, "Export in gexf")
        item_exp_gml = menu.Append(wx.ID_ANY, "Export in gml")
        
        menu.AppendSeparator()

        if not self.db.read_only: 
            item_add_subfamily = menu.Append(wx.ID_ANY, "Add Subfamily")
            menu.AppendSeparator()        
            item_rename = menu.Append(wx.ID_ANY, "Rename Branch")
            item_move = menu.Append(wx.ID_ANY, "Move Branch to Family")
            
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_add_subfamily(item), item_add_subfamily)
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_rename_folder(item), item_rename)
            self.Bind(wx.EVT_MENU, lambda e: self.ctx_move_folder(item), item_move)
            
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_show_family_graph(f_group, include_people=False), item_graph)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_show_family_graph(f_group, include_people=True), item_graph_ppl)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_generate_full_report(target_group=f_group), item_report_grp)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_generate_full_report(target_group=f_group, target_family=node_name), item_report_fam)        


        self.Bind(wx.EVT_MENU, lambda e: self.ctx_export_family_graph(f_group, 'graphml'), item_exp_graphml)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_export_family_graph(f_group, 'gexf'), item_exp_gexf)
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_export_family_graph(f_group, 'gml'), item_exp_gml)

        menu.AppendSeparator()
        item_extract_tree_db = menu.Append(wx.ID_ANY, f"Extract '{node_name}' Branch to Standalone File...")
        self.Bind(wx.EVT_MENU, lambda e: self.ctx_extract_segment_ui(target_group=f_group, target_family=node_name), item_extract_tree_db)
        
        
        self.PopupMenu(menu)
        menu.Destroy()

    def ctx_extract_segment_ui(self, target_group=None, target_family=None):
        """Launches a file selection window offering targeted data slice dumps."""
        scope_lbl = target_family if target_family else target_group
        wildcards = "SQLite3 Database (*.db)|*.db|JSON File (*.json)|*.json"
        
        default_name = f"Segment_{scope_lbl.replace(' ', '_')}.db"
        
        with wx.FileDialog(self, f"Extract {scope_lbl} Scope...", 
                           defaultFile=default_name,
                           wildcard=wildcards, 
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                path = dlg.GetPath()
                success, msg = self.io_helper.export_subsegment_data(path, target_group=target_group, target_family=target_family)
                if success:
                    wx.MessageBox(msg, "Extraction Successful", wx.OK | wx.ICON_INFORMATION)
                else:
                    wx.MessageBox(msg, "Extraction Error", wx.OK | wx.ICON_ERROR)
        
    def ctx_add_subfamily(self, item):
        if self.db.read_only: return
        parent = self.tree.GetItemParent(item)
        family_group = self.tree.GetItemText(parent).replace(" ▾", "").strip()
        if parent == self.root_item:
            family_group = self.tree.GetItemText(item).replace(" ▾", "").strip()
        
        dlg = wx.TextEntryDialog(self, f"Enter new unique nickname for '{family_group}':", "Add Subfamily")
        if dlg.ShowModal() == wx.ID_OK:
            new_nick = dlg.GetValue().strip()
            dprint(f"adding subfamily {family_group}, {new_nick}")
            if new_nick:
                try:
                    self.db.cursor.execute("INSERT INTO families (family_group, family_name) VALUES (?, ?)", 
                                           (family_group, new_nick))
                    self.db.conn.commit()
                    self.refresh_tree()
                except Exception as e:
                    wx.MessageBox(f"Error adding subfamily: {e}", "Database Error", wx.ICON_ERROR)
        dlg.Destroy()

    def ctx_rename_folder(self, item):
        if self.db.read_only: return
        old_name = self.tree.GetItemText(item).replace(" ▾", "").strip()
        dlg = wx.TextEntryDialog(self, "Enter new name for this branch:", "Rename", old_name)
        if dlg.ShowModal() == wx.ID_OK:
            new_name = dlg.GetValue().strip()
            self.db.cursor.execute("UPDATE people SET family_name = ? WHERE family_name = ?", (new_name, old_name))
            self.db.cursor.execute("UPDATE families SET family_name = ? WHERE family_name = ?", (new_name, old_name))
            self.db.conn.commit()
            self.refresh_tree()
        dlg.Destroy()

    def ctx_move_folder(self, item):
        if self.db.read_only: return
        self.db.cursor.execute("SELECT DISTINCT family_group FROM families")
        choices = [r[0] for r in self.db.cursor.fetchall()]
        
        dlg = wx.SingleChoiceDialog(self, "Select target Family Name:", "Move Branch", choices)
        if dlg.ShowModal() == wx.ID_OK:
            target_family = dlg.GetStringSelection()
            old_nick = self.tree.GetItemText(item).replace(" ▾", "").strip()
            
            self.db.cursor.execute("UPDATE people SET family_group = ? WHERE family_name = ?", (target_family, old_nick))
            self.db.conn.commit()
            self.refresh_tree()
        dlg.Destroy()

    def ctx_add_person(self):
        if self.db.read_only: return
        try:
            s = self.tree._selectedItems.pop()
            fn = s.GetData()
            self.db.cursor.execute("INSERT INTO people (name, family_name ) VALUES (?, ?)", ("New", fn))
            new_person_id = self.db.cursor.lastrowid
            self.db.conn.commit()
            self.refresh_tree()
            item = self.find_item_by_id(self.root_item, new_person_id)
            if item: self.tree.SelectItem(item)
            self.load_person_by_id(new_person_id)
            self.fields['name'].SetFocus()
        except Exception as e: wx.MessageBox(f"Failed: {e}", "Error", wx.ICON_ERROR)
        
    def ctx_add_spouse(self, person_id, spouse_type):
        if self.db.read_only: return
        try:
            self.db.cursor.execute("SELECT family_name, surname FROM people WHERE id=?", (person_id,))
            res = self.db.cursor.fetchone()
            f_name, s_name = res[0] if res else "", res[1] if res else ""
            self.db.cursor.execute("INSERT INTO people (name, family_name, surname) VALUES (?, ?, ?)", (f"New {spouse_type.capitalize()}", f_name if spouse_type == "husband" else "", s_name))
            new_spouse_id = self.db.cursor.lastrowid

            if spouse_type == "husband":
                self.db.cursor.execute("SELECT husband_ids FROM people WHERE id = ?", (person_id,))
                existing = self.db.cursor.fetchone()[0]
                new_list = f"{existing}, {new_spouse_id}" if existing else str(new_spouse_id)
                self.db.cursor.execute("UPDATE people SET husband_ids = ? WHERE id = ?", (new_list, person_id))
            else:
                self.db.cursor.execute("SELECT wife_ids FROM people WHERE id = ?", (person_id,))
                existing = self.db.cursor.fetchone()[0]
                new_list = f"{existing}, {new_spouse_id}" if existing else str(new_spouse_id)
                self.db.cursor.execute("UPDATE people SET wife_ids = ? WHERE id = ?", (new_list, person_id))
            self.db.conn.commit()
            
            self.refresh_tree()
            item = self.find_item_by_id(self.root_item, new_spouse_id)
            if item: self.tree.SelectItem(item)
            self.load_person_by_id(new_spouse_id)
            self.fields['name'].SetFocus()
        except Exception as e: wx.MessageBox(f"Failed: {e}", "Error", wx.ICON_ERROR)

    def ctx_add_child(self, parent_id):
        if self.db.read_only: return
        self.db.cursor.execute("SELECT family_name FROM people WHERE id=?", (parent_id,))
        res = self.db.cursor.fetchone()
        self.db.cursor.execute("INSERT INTO people (name, family_name, father_id) VALUES (?, ?, ?)", ("New Child", res[0] if res else "", parent_id))
        self.db.conn.commit()
        new_id = self.db.cursor.lastrowid
        self.refresh_tree()
        item = self.find_item_by_id(self.root_item, new_id)
        if item: self.tree.SelectItem(item)
        self.load_person_by_id(new_id)
        self.fields['name'].SetFocus()

    def ctx_add_parent(self, child_id, parent_type):
        if self.db.read_only: return
        success, parent_id = self.db.add_parent(child_id, parent_type)
        if success: 
            self.refresh_tree()
            item = self.find_item_by_id(self.root_item, parent_id)
            if item: self.tree.SelectItem(item)
            self.load_person_by_id(parent_id)
            self.fields['name'].SetFocus()

    def ctx_unlink_parents(self, person_id):
        if self.db.read_only: return
        if wx.MessageBox("Remove links to parents?", "Confirm", wx.YES_NO) == wx.YES:
            if self.db.unlink_parents(person_id)[0]: self.refresh_tree(); self.load_person_by_id(person_id)

    def ctx_delete_person(self, person_id):
        if self.db.read_only: return
        if wx.MessageBox("Permanently remove record?", "Confirm", wx.YES_NO) == wx.YES:
            if self.db.delete_person_and_unlink_children(person_id)[0]:
                self.current_selected_id = None; self.refresh_tree()

    def ctx_delete_children(self, person_id):
        if self.db.read_only: return
        if wx.MessageBox("Wipe out ALL immediate child nodes?", "Confirm", wx.YES_NO) == wx.YES:
            self.db.delete_children_only(person_id); self.refresh_tree()

    def on_open_db(self, event):
        with wx.FileDialog(self, "Open Database", wildcard="DB files (*.db)|*.db", style=wx.FD_OPEN) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                new_db_path = dlg.GetPath()
                self.db.conn.close()
                self.db = GenealogyData(db=new_db_path, password=None, read_only=self.db.read_only) 
                self.io_helper = GenealogyIO(self.db)
                self.refresh_tree()
                wx.LogStatus(f"Opened database: {new_db_path}")

    def find_relationship(self, person1_input, person2_input, use_kinship=True):
        import networkx as nx

        def resolve_id(user_input):
            input_str = str(user_input).strip()
            if not input_str: return None
            if input_str.isdigit(): return int(input_str)
            
            # v19.44 FIX: Extract ID if formatted as "Name (ID)"
            if "(" in input_str and ")" in input_str:
                try:
                    possible_id = input_str.split("(")[-1].replace(")", "").strip()
                    if possible_id.isdigit(): return int(possible_id)
                except: pass
                
            self.db.cursor.execute("SELECT id FROM people WHERE name = ?", (input_str,))
            res = self.db.cursor.fetchone()
            return res[0] if res else None

        id1 = resolve_id(person1_input)
        id2 = resolve_id(person2_input)

        if not id1 or not id2: 
            return False, "Could not resolve one or both inputs to a valid database record.", "", [], []
        if id1 == id2: 
            return True, "Calculated Relationship: Self\nThey are the exact same person.", "", [], []

        self.db.cursor.execute("PRAGMA table_info(people)")
        cols = [r[1].lower() for r in self.db.cursor.fetchall()]
        if 'sex' in cols:
            self.db.cursor.execute("SELECT id, name, father_id, mother_id, husband_ids, wife_ids, sex FROM people")
        else:
            self.db.cursor.execute("SELECT id, name, father_id, mother_id, husband_ids, wife_ids, '' as sex FROM people")
            
        rows = self.db.cursor.fetchall()

        G = nx.Graph()
        name_map = {}
        sex_map = {}

        for row in rows:
            p_id, name, f_id, m_id, h_ids, w_ids, p_sex = row
            G.add_node(p_id)
            name_map[p_id] = name
            sex_map[p_id] = p_sex

            if f_id: G.add_edge(p_id, f_id, rel='Child-Parent')
            if m_id: G.add_edge(p_id, m_id, rel='Child-Parent')

            for sp_str in [h_ids, w_ids]:
                if sp_str:
                    spouses = [s.strip() for s in str(sp_str).split(',')]
                    for sp in spouses:
                        extracted_id = None
                        if "(" in sp and ")" in sp:
                            try: extracted_id = sp.split("(")[-1].split(")")[0].strip()
                            except: pass
                        elif sp.isdigit(): extracted_id = sp

                        if extracted_id and extracted_id.isdigit():
                            G.add_edge(p_id, int(extracted_id), rel='Spouse')

        try:
            path = nx.shortest_path(G, source=id1, target=id2)
        except nx.NetworkXNoPath:
            msg = f"No relationship path found between {name_map.get(id1, id1)} and {name_map.get(id2, id2)}."
            return True, msg, msg, [], []
        except nx.NodeNotFound:
            return False, "One of the IDs does not exist in the graph structure.", "", [], []

        steps = []
        ups, downs, marriages = 0, 0, 0
        path_sequence = []
        edge_list = []

        for i in range(len(path) - 1):
            curr_node = path[i]
            next_node = path[i+1]
            edge_data = G.get_edge_data(curr_node, next_node)
            rel_type = edge_data.get('rel', 'Connected')

            if rel_type == 'Child-Parent':
                self.db.cursor.execute("SELECT father_id, mother_id FROM people WHERE id = ?", (curr_node,))
                parents = self.db.cursor.fetchone()
                if parents and next_node in parents:
                    rel_desc = "is the child of"
                    ups += 1
                    path_sequence.append('UP')
                    edge_list.append('child of')
                else:
                    rel_desc = "is the parent of"
                    downs += 1
                    path_sequence.append('DOWN')
                    edge_list.append('parent of')
            elif rel_type == 'Spouse':
                rel_desc = "is the spouse of"
                marriages += 1
                path_sequence.append('MAR')
                edge_list.append('spouse of')
            else:
                rel_desc = "is related to"
                edge_list.append('related to')

            steps.append(f"{name_map[curr_node]}  --({rel_desc})-->  {name_map[next_node]}")

        target_person_sex = sex_map.get(id2)
        kinship_term = self.get_kinship_term(ups, downs, marriages, path_sequence, target_sex=target_person_sex)
        
        report_kinship = f"Calculated Relationship: {kinship_term}"
        report_standard = f"Path ({len(path)-1} degrees of separation):\n\n" + "\n".join(steps)
        path_names = [name_map[n] for n in path]
            
        return True, report_kinship, report_standard, path_names, edge_list

    def show_relationship_graph(self, kinship_rep, std_rep, path_names, edge_list):
        """Renders an interactive split-pane view displaying the network path and text report."""
        import matplotlib.pyplot as plt
        import networkx as nx
        
        path_length = len(path_names)
        dynamic_height = max(6.0, path_length * 1.5)
        
        fig = plt.figure(figsize=(12, dynamic_height))
        fig.canvas.manager.set_window_title("Relationship Visualization")
        
        ax1 = plt.subplot(1, 2, 1)
        ax1.set_title("Visual Path", fontweight='bold', color='#2c3e50', fontsize=14)
        
        path_G = nx.DiGraph()
        pos = {}
        labels = {}
        
        for i, node_name in enumerate(path_names):
            path_G.add_node(i)
            pos[i] = (0, -i) 
            labels[i] = node_name.replace(" ", "\n") if len(node_name) > 15 else node_name
            
        for i in range(len(path_names)-1):
            path_G.add_edge(i, i+1)
            
        nx.draw_networkx_nodes(path_G, pos, ax=ax1, node_size=2800, node_color='#3498db', edgecolors='black', linewidths=1.5)
        nx.draw_networkx_edges(path_G, pos, ax=ax1, arrowstyle='->', arrowsize=25, edge_color='gray', width=2.5)
        nx.draw_networkx_labels(path_G, pos, labels, ax=ax1, font_size=9, font_weight='bold', font_color='black')
        
        # v19.48: Rotation removed to default to horizontal reading
        for i in range(len(path_names)-1):
            x = (pos[i][0] + pos[i+1][0]) / 2
            y = (pos[i][1] + pos[i+1][1]) / 2
            ax1.text(x, y, edge_list[i], color='green', fontsize=10, fontweight='bold',
                     ha='center', va='center',
                     bbox=dict(facecolor='white', edgecolor='none', alpha=0.8, pad=0.5))
                     
        ax1.set_xlim(-1, 1)
        ax1.set_ylim(-len(path_names), 1)
        ax1.axis('off')
        
        ax2 = plt.subplot(1, 2, 2)
        ax2.axis('off')
        
        ax2.text(0.0, 0.95, kinship_rep, fontsize=15, fontweight='bold', color='#2ecc71', transform=ax2.transAxes)
        ax2.text(0.0, 0.85, std_rep, fontsize=11, family='monospace', verticalalignment='top', transform=ax2.transAxes)
        
        plt.tight_layout()
        plt.show()
        
    
    def get_kinship_term(self, ups, downs, marriages, path_sequence, target_sex=None):
        """Translates graph traversal steps into natural English relationship terms based on sex."""
        sex_str = str(target_sex).strip().upper() if target_sex else ""
        is_m = sex_str in ['M', 'MALE']
        is_f = sex_str in ['F', 'FEMALE']

        standard = True
        seen_down = False
        for step in path_sequence:
            if step == 'DOWN':
                seen_down = True
            elif step == 'UP' and seen_down:
                standard = False
                break
                
        if not standard and marriages == 0: return "Distant/Complex Blood Relative"
        if marriages > 1: return "Extended In-Law / Complex Connection"

        base_term = ""
        if ups == 0 and downs == 0:
            if marriages == 1: return "Husband" if is_m else "Wife" if is_f else "Spouse"
            return "Self"
        elif ups == 1 and downs == 0: base_term = "Father" if is_m else "Mother" if is_f else "Parent"
        elif ups == 2 and downs == 0: base_term = "Grandfather" if is_m else "Grandmother" if is_f else "Grandparent"
        elif ups > 2 and downs == 0: 
            prefix = 'Great-' * (ups - 2)
            base_term = f"{prefix}Grandfather" if is_m else f"{prefix}Grandmother" if is_f else f"{prefix}Grandparent"
        elif ups == 0 and downs == 1: base_term = "Son" if is_m else "Daughter" if is_f else "Child"
        elif ups == 0 and downs == 2: base_term = "Grandson" if is_m else "Granddaughter" if is_f else "Grandchild"
        elif ups == 0 and downs > 2: 
            prefix = 'Great-' * (downs - 2)
            base_term = f"{prefix}Grandson" if is_m else f"{prefix}Granddaughter" if is_f else f"{prefix}Grandchild"
        elif ups == 1 and downs == 1: base_term = "Brother" if is_m else "Sister" if is_f else "Sibling"
        elif ups >= 2 and downs == 1:
            if ups == 2: base_term = "Uncle" if is_m else "Aunt" if is_f else "Aunt / Uncle"
            else: 
                prefix = 'Great-' * (ups - 3)
                base_term = f"{prefix}Great-Uncle" if is_m else f"{prefix}Great-Aunt" if is_f else f"{prefix}Great-Aunt / Uncle"
        elif ups == 1 and downs >= 2:
            if downs == 2: base_term = "Nephew" if is_m else "Niece" if is_f else "Niece / Nephew"
            else: 
                prefix = 'Great-' * (downs - 3)
                base_term = f"{prefix}Great-Nephew" if is_m else f"{prefix}Great-Niece" if is_f else f"{prefix}Great-Niece / Nephew"
        elif ups >= 2 and downs >= 2:
            cousin_degree = min(ups, downs) - 1
            removed = abs(ups - downs)
            ordinal = lambda n: "%d%s" % (n, "tsnrhtdd"[(n//10%10!=1)*(n%10<4)*n%10::4]) 
            base_term = f"{ordinal(cousin_degree)} Cousin"
            if removed > 0: base_term += f" {removed}x removed"

        if marriages == 1:
            if path_sequence[0] == 'MAR' and base_term:
                if "Cousin" not in base_term: return f"{base_term}-in-law"
                return f"Spouse's {base_term}"
            elif path_sequence[-1] == 'MAR' and base_term:
                if "Cousin" not in base_term: return f"{base_term}-in-law"
                return f"{base_term}'s Spouse"
            else:
                return "Step-Relative / Complex In-Law"
                
        return base_term


    def on_edit_families(self, event):
        with EditFamiliesDialog(self, self.db) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.refresh_tree()
                
    def on_edit_people(self, event):
        with EditPeopleDialog(self, self.db) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.refresh_tree()
    
    def on_find_relationship_ui(self, event):
        selected_ids = []
        mode = self.view_mode.GetSelection()
        
        # --- v19.47 MODE-AWARE SELECTION ENGINE ---
        if mode == 1:  # 1. People List Mode (Prioritize Left Panel)
            item = self.list_view.GetFirstSelected()
            while item != -1:
                selected_ids.append(self.list_view.GetItemData(item))
                item = self.list_view.GetNextSelected(item)
                
            # Fallback to middle panel if nothing is selected on the left
            if not selected_ids:
                item = self.fm_list_view.GetFirstSelected()
                while item != -1:
                    selected_ids.append(self.fm_list_view.GetItemData(item))
                    item = self.fm_list_view.GetNextSelected(item)
                    
        else:  # 2. Tree View (0) or Family List (2) Mode (Prioritize Middle Panel)
            item = self.fm_list_view.GetFirstSelected()
            while item != -1:
                selected_ids.append(self.fm_list_view.GetItemData(item))
                item = self.fm_list_view.GetNextSelected(item)
                
        # 3. Universal Fallback (Catches the single actively viewed profile)
        if not selected_ids and self.current_selected_id:
            selected_ids.append(self.current_selected_id)
        # ------------------------------------------
            
        p1_text, p2_text = "", ""
        
        if len(selected_ids) >= 1:
            self.db.cursor.execute("SELECT name FROM people WHERE id = ?", (selected_ids[0],))
            res = self.db.cursor.fetchone()
            if res: p1_text = f"{res[0]} ({selected_ids[0]})"
            
        if len(selected_ids) >= 2:
            self.db.cursor.execute("SELECT name FROM people WHERE id = ?", (selected_ids[1],))
            res = self.db.cursor.fetchone()
            if res: p2_text = f"{res[0]} ({selected_ids[1]})"
            
        dlg = wx.Dialog(self, title="Find Relationship", size=(450, 200))
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        hbox1 = wx.BoxSizer(wx.HORIZONTAL)
        st1 = wx.StaticText(dlg, label="First Person (Name or ID):")
        tc1 = wx.TextCtrl(dlg, value=p1_text)
        hbox1.Add(st1, flag=wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, border=8)
        hbox1.Add(tc1, proportion=1)
        
        hbox2 = wx.BoxSizer(wx.HORIZONTAL)
        st2 = wx.StaticText(dlg, label="Second Person (Name or ID):")
        tc2 = wx.TextCtrl(dlg, value=p2_text)
        hbox2.Add(st2, flag=wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, border=8)
        hbox2.Add(tc2, proportion=1)
        
        btn_sizer = dlg.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(hbox1, flag=wx.EXPAND | wx.ALL, border=15)
        vbox.Add(hbox2, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)
        vbox.Add(btn_sizer, flag=wx.EXPAND | wx.ALL, border=15)
        
        dlg.SetSizer(vbox)
        dlg.Layout()
        
        if dlg.ShowModal() == wx.ID_OK:
            p1 = tc1.GetValue().strip()
            p2 = tc2.GetValue().strip()
            
            if not p1 or not p2:
                wx.MessageBox("Both fields are required.", "Input Error", wx.ICON_WARNING)
            else:
                # Passes calculation to the Background IO Helper
                success, kinship_rep, std_rep, path_names, edge_list = self.io_helper.find_relationship(p1, p2)
                if not success:
                    wx.MessageBox(kinship_rep, "Calculation Error", wx.OK | wx.ICON_ERROR)
                elif not path_names: 
                    wx.MessageBox(kinship_rep, "Relationship Result", wx.OK | wx.ICON_INFORMATION)
                else:
                    self.show_relationship_graph(kinship_rep, std_rep, path_names, edge_list)
                    
        dlg.Destroy()


    def on_export_sql(self, event):
        with wx.FileDialog(self, "Export SQL Dump", wildcard="SQL files (*.sql)|*.sql", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                success, msg = self.io_helper.export_to_sql(dlg.GetPath())
                wx.MessageBox(msg, "SQL Export Result" if success else "Error")
                
    def on_import_sql(self, event):
        if self.db.read_only: return
        with wx.FileDialog(self, "Select SQL Dump File", wildcard="SQL files (*.sql)|*.sql", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                if wx.MessageBox("WARNING: Importing a full SQL dump may overwrite existing data or cause duplication if not properly formatted. Proceed?", "Confirm SQL Execution", wx.YES_NO | wx.ICON_WARNING) == wx.YES:
                    success, msg = self.io_helper.import_from_sql(dlg.GetPath())
                    if success: self.refresh_tree()
                    wx.MessageBox(msg, "SQL Import")
                
def get_db_password():
    config_file = "config.json"
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
                if "db_password" in data: return data["db_password"]
        except: pass
    dlg = PasswordDialog(None)
    if dlg.ShowModal() == wx.ID_OK:
        password = dlg.password_ctrl.GetValue()
        if dlg.save_cb.IsChecked():
            with open(config_file, 'w') as f: json.dump({"db_password": password}, f, indent=4)
        return password
    return None

def load_saved_db_name():
    """Reads the fallback database path from config.json, or checks for a local family.db file."""
    config_file = "config.json"
    
    # Tier 1: Look for an explicit saved path parameter array inside the config file
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
                saved_path = data.get("last_db_path")
                if saved_path:
                    return saved_path
        except:
            pass
            
    # Tier 2: If config has no valid key, scan the current working path for family.db
    if os.path.exists("family.db"):
        return "family.db"
        
    # Tier 3: Return None to signal that a brand-new file structure must be initialized
    return None

def update_saved_db_name(db_path):
    """Saves or updates the active database tracking reference inside config.json."""
    config_file = "config.json"
    data = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
        except:
            pass

def load_saved_settings():
    """Reads the fallback database path and engine preference from config.json,

    or falls back to checking for a local family.db file.
    """
    config_file = "config.json"
    saved_path = None
    saved_engine = "sqlite3"  # Default fallback engine baseline
    
    # Tier 1: Inspect config.json for explicit saved properties
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
                saved_path = data.get("last_db_path")
                saved_engine = data.get("last_engine_type", "sqlite3")
                if saved_path:
                    return saved_path, saved_engine
        except:
            pass
            
    # Tier 2: Check if family.db exists locally in the workspace folder
    if os.path.exists("family.db"):
        return "family.db", "sqlite3"
        
    # Tier 3: Return None for path, defaulting to sqlite3 as the creation engine
    return None, "sqlite3"

def update_saved_settings(db_path, engine_type):
    """Saves or updates the active database path and engine type inside config.json."""
    config_file = "config.json"
    data = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
        except:
            pass
            
    data["last_db_path"] = db_path
    data["last_engine_type"] = engine_type
    try:
        with open(config_file, 'w') as f:
            json.dump(data, f, indent=4)
    except:
        pass

        
class Database:
    def __init__(self, db_path, engine_type='sqlite3', password=None):
        self.db_path = db_path
        self.conn = self._initialize_engine(engine_type, db_path, password)
        self.cursor = self.conn.cursor()

    def _initialize_engine(self, engine_type, db_path, password):
        if engine_type == 'sqlcipher':
            try:
                import sqlcipher3 as sqlite3
            except ImportError:
                dprint("Error: sqlcipher3 not found. Install with 'pip install sqlcipher3-binary'")
                exit(1)
            
            conn = sqlite3.connect(db_path)
            if password:
                conn.execute(f"PRAGMA key = '{password}'")
            return conn
        else:
            import sqlite3
            return sqlite3.connect(db_path)

    def commit(self):
        self.conn.commit()

    def execute(self, query, params=()):
        return self.cursor.execute(query, params)


def run_cli_mode(db, args):
    import os
    import re
    
    # Instantiate the IO Helper for all import/export tasks
    io_helper = GenealogyIO(db)
    
    # --- 1. YOUR ORIGINAL CLI LOGIC (LEGACY EXPORT) ---
    if getattr(args, 'output', None) and getattr(args, 'format', None):
        fmt = args.format.lower()
        if fmt == 'csv':
            success, msg = io_helper.export_to_csv(args.output)
        elif fmt == 'gedcom':
            success, msg = io_helper.export_to_gedcom(args.output)
        elif fmt == 'json':
            success, msg = io_helper.export_to_json(args.output)
        else:
            success, msg = False, "Unknown format"
        
        if success:
            dprint(f"Operation complete: Exported to {args.output} in {args.format} format.")
        else:
            dprint(f"Error during export: {msg}")
            
    elif getattr(args, 'output', None) or getattr(args, 'format', None):
        dprint("Error: CLI mode requires --output and --format for export operations.")
        return

    print("\n[+] Initializing Genealogy Headless Extensions...")

    # --- 2. NEW HEADLESS SESSION DEFINITION ---
    class HeadlessSession:
        def __init__(self, active_db):
            self.db = active_db
            
        # Map the Report Generator
        from report_generator import ctx_generate_full_report
        generate_report = ctx_generate_full_report

        # Headless Graph Exporter
        def export_headless_graph(self, family_group, file_format):
            import networkx as nx
            
            self.db.cursor.execute("SELECT family_name, ancestral_family_name FROM families WHERE family_group = ?", (family_group,))
            records = list(set(self.db.cursor.fetchall()))
            if not records:
                print(f"[-] Error: No family branches found for group '{family_group}'.")
                return

            G = nx.DiGraph()
            for f_name, a_name in records:
                parent = a_name if a_name and a_name.strip() else family_group
                if parent != f_name:
                    G.add_edge(parent, f_name)
                else:
                    G.add_node(f_name)

            safe_group = re.sub(r'[^a-zA-Z0-9]', '_', family_group)
            filepath = f"CLI_Export_{safe_group}_network.{file_format}"
            
            if file_format == 'graphml': nx.write_graphml(G, filepath)
            elif file_format == 'gexf': nx.write_gexf(G, filepath)
            elif file_format == 'gml': nx.write_gml(G, filepath)
            print(f"[+] Successfully exported graph to: {os.path.abspath(filepath)}")

    cli = HeadlessSession(db)

    # --- 3. PROCESS NEW AUTOMATED COMMANDS ---
    if getattr(args, 'import_file', None):
        filepath = args.import_file
        print(f"[*] Starting import from {filepath}...")
        if filepath.lower().endswith('.vcf'):
            count = io_helper.import_vcard(filepath)
            print(f"[+] Import complete: {count} contacts loaded.")
        elif filepath.lower().endswith('.ged'):
            success, msg = io_helper.import_from_gedcom(filepath)
            print(f"[+] Import complete: {msg}")
        elif filepath.lower().endswith('.json'):
            try:
                io_helper.import_from_json(filepath)
                print(f"[+] Import complete: Successfully imported JSON.")
            except Exception as e:
                print(f"[-] Import failed: {e}")
        # ... inside getattr(args, 'import_file', None):
        elif filepath.lower().endswith('.sql'):
            success, msg = io_helper.import_from_sql(filepath)
            print(f"[+] Import complete: {msg}")
            
        # ... inside getattr(args, 'export_data', None):
        elif filepath.lower().endswith('.sql'):
            success, msg = io_helper.export_to_sql(filepath)
            print(f"[+] Export complete: {msg}")        
        else:
            # Assume it is a CSV base path
            p_path = f"{filepath}_people.csv"
            f_path = f"{filepath}_families.csv"
            success, msg = io_helper.import_from_csv(p_path, f_path)
            print(f"[+] Import complete: {msg}")            

    if getattr(args, 'export_data', None):
        filepath = args.export_data
        print(f"[*] Exporting database to {filepath}...")
        if filepath.lower().endswith('.vcf'):
            count = io_helper.export_vcard(filepath)
            print(f"[+] Export complete: {count} contacts saved.")
        elif filepath.lower().endswith('.ged'):
            success, msg = io_helper.export_to_gedcom(filepath)
            print(f"[+] Export complete: {msg}")
        elif filepath.lower().endswith('.json'):
            success, msg = io_helper.export_to_json(filepath)
            print(f"[+] Export complete: {msg}")
        else:
            # Assume it is a CSV base path
            success, msg = io_helper.export_to_csv(filepath)
            print(f"[+] Export complete: {msg}")

    if getattr(args, 'export_graph', None):
        if not getattr(args, 'group', None):
            print("[-] Error: You must specify a --group to export a graph.")
        else:
            cli.export_headless_graph(args.group, args.export_graph)

    if getattr(args, 'report', None):
        print("[*] Compiling Report...")

        target_grp = getattr(args, 'group', None)
        
        # If the user specifically types "All Families" (the UI root node) 
        # or leaves it completely blank, treat it as a master global report.
        if target_grp and target_grp.strip().lower() == "all families":
            target_grp = None                    
        cli.generate_report(target_group=target_grp, target_family=getattr(args, 'branch', None))
        
    if getattr(args, 'relationship', None):
        print(f"[*] Calculating Relationship Graph...")
        
        parts = args.relationship.split(',')
        if len(parts) != 2:
            print("[-] Error: --relationship requires two IDs separated by a comma (e.g., '12,45')")
        else:
            p1, p2 = parts[0].strip(), parts[1].strip()
            success, kinship_rep, std_rep, path_names, edge_list = io_helper.find_relationship(p1, p2)
            
            if not success:
                print(f"[-] Error: {kinship_rep}")
            elif not path_names:
                print(f"[*] {kinship_rep}")
            else:
                fmt = getattr(args, 'format', 'png') or 'png'
                out_path = getattr(args, 'output', None) or f"relationship_{p1}_{p2}.{fmt}"
                
                if fmt.lower() in ['jpg', 'jpeg', 'png']:
                    import matplotlib.pyplot as plt
                    import networkx as nx
                    
                    path_length = len(path_names)
                    dynamic_height = max(6.0, path_length * 1.5)
                    
                    fig = plt.figure(figsize=(12, dynamic_height))
                    ax1 = plt.subplot(1, 2, 1)
                    ax1.set_title("Visual Path", fontweight='bold', color='#2c3e50', fontsize=14)
                    
                    path_G = nx.DiGraph()
                    pos = {}
                    labels = {}
                    for i, node_name in enumerate(path_names):
                        path_G.add_node(i)
                        pos[i] = (0, -i) 
                        labels[i] = node_name.replace(" ", "\n") if len(node_name) > 15 else node_name
                        
                    for i in range(len(path_names)-1):
                        path_G.add_edge(i, i+1)
                        
                    nx.draw_networkx_nodes(path_G, pos, ax=ax1, node_size=2800, node_color='#3498db', edgecolors='black', linewidths=1.5)
                    nx.draw_networkx_edges(path_G, pos, ax=ax1, arrowstyle='->', arrowsize=25, edge_color='gray', width=2.5)
                    nx.draw_networkx_labels(path_G, pos, labels, ax=ax1, font_size=9, font_weight='bold', font_color='black')
                    
                    # v19.48: Rotation removed to default to horizontal reading
                    for i in range(len(path_names)-1):
                        x = (pos[i][0] + pos[i+1][0]) / 2
                        y = (pos[i][1] + pos[i+1][1]) / 2
                        ax1.text(x, y, edge_list[i], color='green', fontsize=10, fontweight='bold',
                                 ha='center', va='center',
                                 bbox=dict(facecolor='white', edgecolor='none', alpha=0.8, pad=0.5))
                                 
                    ax1.set_xlim(-1, 1)
                    ax1.set_ylim(-len(path_names), 1)
                    ax1.axis('off')
                    
                    ax2 = plt.subplot(1, 2, 2)
                    ax2.axis('off')
                    ax2.text(0.0, 0.95, kinship_rep, fontsize=15, fontweight='bold', color='#2ecc71', transform=ax2.transAxes)
                    ax2.text(0.0, 0.85, std_rep, fontsize=11, family='monospace', verticalalignment='top', transform=ax2.transAxes)
                    
                    plt.tight_layout()
                    plt.savefig(out_path, format=fmt.lower(), dpi=150)
                    plt.close()
                    print(f"[+] Relationship graph saved to {out_path}")

                    
                elif fmt.lower() == 'tex':
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write("\\documentclass{article}\n\\begin{document}\n")
                        f.write(f"\\section*{{Relationship Report}}\n")
                        f.write(f"\\textbf{{{kinship_rep}}}\\\\\n\n")
                        f.write("\\begin{verbatim}\n")
                        f.write(std_rep)
                        f.write("\n\\end{verbatim}\n")
                        f.write("\\end{document}")
                    print(f"[+] Relationship TeX file saved to {out_path}")
                else:
                    print(f"[-] Unsupported format: {fmt}")



def parse_arguments():
    parser = argparse.ArgumentParser(description="Genealogy Data Utility")
    parser.add_argument("-p", "--password", help="Password for SQLCipher database")
    parser.add_argument("-e", "--engine", choices=['sqlite3', 'sqlcipher'], default='sqlite3', help="Engine: sqlite3 (default) or sqlcipher")
    parser.add_argument("-d", "--db", default=None, help="Path to database file")
    parser.add_argument("-v", "--verbose", nargs='?', type=int, const=2, default=0, 
                        help="Enable debug logging (optional level int, defaults to 2 if flag used)")
    parser.add_argument("-i", "--input", help="Input file path (Legacy)")
    parser.add_argument("-o", "--output", help="Output file path (Legacy)")
    parser.add_argument("-f", "--format", choices=['csv', 'gedcom', 'json', 'jpg', 'jpeg', 'png', 'tex'], help="File format for export or graphing")
    parser.add_argument("-r", "--readonly", action='store_true', help="Boot up database structure in safe Read-Only mode")
    parser.add_argument("--debug-panel", action='store_true', help="Boot up with the unified bottom interactive debugger shell active")
    
    parser.add_argument("--report", action="store_true", help="Generate a family report")
    parser.add_argument("--group", type=str, help="Specify a target Family Group")
    parser.add_argument("--branch", type=str, help="Specify a target Sub-Branch")
    parser.add_argument("--export-graph", choices=['graphml', 'gexf', 'gml'], help="Export a network graph for a group")
    parser.add_argument("--relationship", type=str, help="Calculate relationship. Usage: id1,id2")    
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-c", "--cli", action='store_true', help="Command line only mode")
    group.add_argument("-g", "--gui", action='store_true', default=True, help="Graphical user interface (default)")
    
    parser.add_argument("--import-file", type=str, help="Import data. Usage: file.ged, .json, .vcf, .sql, or 'basepath' for CSVs")
    parser.add_argument("--export-data", type=str, help="Export data. Usage: file.ged, .json, .vcf, .sql, or 'basepath' for CSVs")
    
    return parser.parse_args()

def main():
    global debug 
    args = parse_arguments() #

    debug = args.verbose    
    
    if debug > 0:
        debug = 2
        print("[*] Verbose logging enabled (debug level {debug})")   
    # Auto-detect headless CLI mode
    is_cli_task = args.cli or args.report or getattr(args, 'export_data', None) or getattr(args, 'import_file', None) or getattr(args, 'export_graph', None) #
    if is_cli_task:
        args.gui = False #
        
    # LINEAGE RESOLUTION FOR v19.89
    target_db_file = args.db
    
    # We check if the argument parser flag matches the default state
    # If the user explicitly typed '-e sqlcipher', we respect it over the config fallback
    engine_specified = '--engine' in sys.argv or '-e' in sys.argv
    target_engine = args.engine
    
    if not target_db_file:
        # Load both values from tracking config or local folder scans
        config_path, config_engine = load_saved_settings()
        target_db_file = config_path
        if not engine_specified:
            target_engine = config_engine
            
    if not target_db_file:
        if not args.gui: #
            print("[-] Error: Headless operation aborted. No database specified.") #
            return #
        else: #
            target_db_file = "family.db" #
            target_engine = "sqlite3"  # Explicitly enforce sqlite3 for fresh creations
            print(f"[*] Initializing brand-new {target_engine} database at: './{target_db_file}'") #
            
    # Persist the finalized configuration values back to settings
    update_saved_settings(target_db_file, target_engine)
    
    try:
        # Connect using the cleanly resolved database path and engine type definitions
        db = Database(db_path=target_db_file, engine_type=target_engine, password=args.password)
    except Exception as e:
        dprint(f"Failed to connect to database: {e}") 
        return 

    if args.gui: 
        app = wx.App() 
        
        # === v19.96 FIX: Intelligent Password Prompting ===
        pwd = args.password
        
        # Only summon the UI password dialog if the engine actually requires encryption
        if target_engine == 'sqlcipher' and pwd is None:
            pwd = get_db_password()
            if not pwd:
                # If they cancel the dialog on an encrypted DB, abort the launch safely
                wx.MessageBox("Password required to access encrypted database.", "Access Denied", wx.ICON_ERROR)
                app.ExitMainLoop()
                return
                
        # If the engine is sqlite3, pwd safely remains None and bypasses the dialog entirely!
        frame = GenealogyFrame(
            engine_type=target_engine,
            db=target_db_file, 
            db_password=pwd, 
            read_only=args.readonly,
            show_debug_panel=args.debug_panel
        )
        frame.Show()
        app.MainLoop()
        
    else: 
        run_cli_mode(db, args) 

        
if __name__ == "__main__":
    main()
