import os
import datetime
import re
import shutil
import base64
import urllib.request
import networkx as nx
import matplotlib.pyplot as plt


def ctx_generate_full_report(self, target_group=None, target_family=None, member_table_cols=None, personal_table_cols=None):
    ####
    confidential_fields = [
        'dob', 'date_of_birth', 'birthdate', 'birth_date',
        'phone', 'other_phones', 'whatsapp', 'telegram',
        'notes', 'conf_notes'    
        # 'home_address', 'home_contact'
        # conf_notes
    ]


    ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    timestamp_str = ist_now.strftime('%Y%m%d_%H%M%S')
    display_time = ist_now.strftime('%B %d, %Y at %I:%M %p IST')
    
    try:
        import qrcode
        has_qrcode = True
    except ImportError:
        has_qrcode = False
        print("WARNING: 'qrcode' library not found. VCard QR generation will be skipped.")

    if target_family:
        groups = [str(target_group).strip()] if target_group else []
        safe_target = re.sub(r'[^a-zA-Z0-9]', '_', target_family)
        folder_name = f"FamilyTree_{safe_target}_Branch_Report_{timestamp_str}"
        report_title = f"{target_family} Branch Report"
    elif target_group:
        groups = [str(target_group).strip()]
        safe_target = re.sub(r'[^a-zA-Z0-9]', '_', target_group)
        folder_name = f"FamilyTree_{safe_target}_Report_{timestamp_str}"
        report_title = f"{target_group} Family Report"
    else:
        self.db.cursor.execute("SELECT DISTINCT family_group FROM families WHERE family_group IS NOT NULL AND family_group != ''")
        raw_groups = [r[0] for r in self.db.cursor.fetchall()]
        groups = sorted(list(set([str(g).strip() for g in raw_groups if g])))
        folder_name = f"FamilyTree_Global_Report_{timestamp_str}"
        report_title = "Complete Family Tree Report"

    if not groups:
        print("INFO: No family groups found to generate a report.")
        return


    print(f"\n--- Starting Report Generation: {len(groups)} groups identified ---")
    os.makedirs(folder_name, exist_ok=True)

    html_header = [
        "<html><head><meta charset='utf-8'><title>Family Tree Report</title>",
        "<style>body { font-family: Arial, sans-serif; margin: 40px; color: #333; background-color: #fcfcfc;} ",
        "h1 { border-bottom: 2px solid #2c3e50; padding-bottom: 10px; } ",
        "h2 { color: #2c3e50; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; } ",
        "h3 { color: #2980b9; margin-top: 25px; font-size: 16px; background: #e8f4f8; padding: 8px; border-radius: 4px; } ",
        "table { border-collapse: collapse; width: 100%; margin-top: 10px; margin-bottom: 20px; font-size: 14px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.05); } ",
        "th, td { border: 1px solid #bdc3c7; padding: 8px 12px; text-align: left; vertical-align: middle; } ",
        "th { background-color: #2c3e50; color: white; } ",
        "a { color: inherit; text-decoration: none; border-bottom: 1px dashed; } ",
        "a:hover { color: #2980b9; border-bottom: 1px solid #2980b9; } ",
        "img { max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px; padding: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; background: white; } ",
        ".toc { background: #fff; border: 1px solid #bdc3c7; padding: 20px; border-radius: 5px; margin-bottom: 40px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); } ",
        ".toc h2 { margin-top: 0; padding-top: 0; border-top: none; } ",
        ".toc ul { list-style-type: none; padding-left: 20px; } ",
        ".toc > ul { padding-left: 0; } ",
        ".toc li { margin-bottom: 8px; } ",
        ".toc a { text-decoration: none; color: #2980b9; border-bottom: none; font-weight: bold; } ",
        ".toc a:hover { text-decoration: underline; } ",
        ".toc ul ul a { font-weight: normal; color: #34495e; } ",
        ".scroll-box { width: 100%; max-height: 800px; overflow: auto; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 20px; background: #eaeaea; } ",
        ".scroll-box img { max-width: none; } </style>",
        "</head><body>",
        f"<h1>{report_title}</h1><p><em>Generated on {display_time}</em></p>"
    ]

    latex_content = [
        r"\documentclass{article}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage{graphicx}",
        r"\usepackage{longtable}",
        r"\usepackage{multicol}",  
        r"\usepackage[a4paper, margin=1in]{geometry}",
        r"\usepackage{xcolor}",  
        r"\usepackage[hidelinks, colorlinks=true, linkcolor=blue, urlcolor=blue]{hyperref}", 
        r"\begin{document}",
        rf"\title{{{report_title}}}",
        rf"\date{{Generated on {display_time}}}",
        r"\maketitle",
        r"\tableofcontents",
        r"\newpage"
    ]
    
    latex_profiles_content = [r"\newpage", r"\section{Detailed Personal Profiles}"]
    generated_profiles = set()
    media_cache = {} 
    
    global_families = []
    global_persons = []
    
    toc_lines = ["<div class='toc'><h2>Table of Contents</h2><ul>"]
    body_html = []

    self.db.cursor.execute("PRAGMA table_info(people)")
    columns = [row[1].lower() for row in self.db.cursor.fetchall()]
    
    id_col = next((c for c in columns if 'id' in c), columns[0])
    name_col = next((c for c in columns if 'name' in c and 'family' not in c), columns[1] if len(columns)>1 else columns[0])
    
    try:
        self.db.cursor.execute(f"SELECT {id_col}, {name_col} FROM people")
        people_id_map = {str(r[0]).strip(): str(r[1]).strip() for r in self.db.cursor.fetchall()}
    except Exception as e:
        people_id_map = {}

    try:
        self.db.cursor.execute("SELECT family_id, family_name FROM families")
        family_id_map = {str(r[0]).strip(): str(r[1]).strip() for r in self.db.cursor.fetchall()}
    except Exception as e:
        family_id_map = {}

    fam_col = next((c for c in columns if c == 'family_name'), 'family_name')
    anc_col = next((c for c in columns if 'ancestral' in c), None)
    
    type_col = next((c for c in columns if c in ['type', 'relation', 'relationship', 'role']), None)
    age_col = next((c for c in columns if c in ['age', 'current_age']), None)
    dob_col = next((c for c in columns if c in ['dob', 'date_of_birth', 'birthdate']), None)
    status_col = next((c for c in columns if c == 'status'), None)
    moved_col = next((c for c in columns if 'moved' in c), None)
    departed_col = next((c for c in columns if 'depart' in c or 'deceas' in c or 'dead' in c), None)
    
    photo_col = next((c for c in columns if c == 'local_photo_path'), None)
    if not photo_col:
        photo_col = next((c for c in columns if any(k in c for k in ['photo', 'image', 'picture', 'pic', 'avatar'])), None)

    # Calculate dynamic display columns for the Summary Tables
    if member_table_cols is not None:
        display_cols = [c.lower() for c in member_table_cols if c.lower() in columns and c.lower() not in confidential_fields]
    else:
        display_cols = [name_col, 'computed_age', type_col, fam_col, anc_col, status_col]
        display_cols = [c for c in display_cols if c is not None and c not in confidential_fields]

    # Calculate dynamic display columns for the Individual Profiles
    if personal_table_cols is not None:
        profile_display_cols = [c.lower() for c in personal_table_cols if c.lower() in columns and c.lower() not in confidential_fields]
    else:
        exclude_cols = [c for c in [photo_col] if c is not None] + confidential_fields
        profile_display_cols = [c for c in columns if c not in exclude_cols]

    # Explicitly inject the computed_age field back into the profiles so it displays
    # even if the raw DOB data was suppressed by the confidential list.
    if 'computed_age' not in profile_display_cols: 
        profile_display_cols.insert(1, 'computed_age')

    # Ensure dynamic relationship columns exist in the profile list
    for dyn_col in ['brothers', 'sisters', 'siblings', 'derived_sons', 'derived_daughters', 'derived_children']:
        if dyn_col not in profile_display_cols: 
            profile_display_cols.append(dyn_col)
        
    def escape_latex(text):
        if text is None: return ""
        text = str(text)
        for char, repl in [('&', r'\&'), ('%', r'\%'), ('$', r'\$'), ('#', r'\#'), ('_', r'\_'), ('{', r'\{'), ('}', r'\}')]:
            text = text.replace(char, repl)
        text = text.replace('\n', r'\newline ')
        return text
        
    def get_clean_id(val):
        v = str(val).strip()
        if v.isdigit(): return v
        if '(' in v and ')' in v:
            try:
                ex = v.split('(')[-1].split(')')[0].strip()
                if ex.isdigit(): return ex
            except: pass
        return None

    def calculate_age(row_data, is_dead):
        if is_dead: return ""
        dob_val = ""
        for k in ['dob', 'date_of_birth', 'birthdate', 'birth_date']:
            if k in row_data and row_data[k] and str(row_data[k]).strip():
                dob_val = str(row_data[k]).strip()
                break
        
        if not dob_val:
            if 'age' in row_data and row_data['age'] and str(row_data['age']).strip().isdigit():
                return str(row_data['age']).strip()
            return ""

        match = re.search(r'\b(18|19|20)\d{2}\b', dob_val)
        if match:
            b_year = int(match.group(0))
            age = ist_now.year - b_year
            if 0 <= age <= 120:
                return str(age)
        return ""

    def extract_media(data, prefix, pid, p_name):
        if data is None: return ""
        safe_pid = str(pid).replace('_', '-')
        
        if isinstance(data, (bytes, bytearray, memoryview)):
            b_data = bytes(data)
            ext = ".png"
            if b_data.startswith(b'\xFF\xD8'): ext = ".jpg"
            elif b_data.startswith(b'\x89PNG'): ext = ".png"
            elif b_data.startswith(b'GIF'): ext = ".gif"
            elif b_data.startswith(b'RIFF') and b'WEBP' in b_data[8:12]: ext = ".webp"
            
            fname = f"{prefix}_{safe_pid}{ext}"
            with open(os.path.join(folder_name, fname), "wb") as f:
                f.write(b_data)
            return fname
            
        if isinstance(data, str):
            data_str = data.strip()
            if not data_str or data_str.lower() in ['none', 'null']: return ""
            
            if data_str.startswith("http"):
                try:
                    ext = os.path.splitext(data_str.split('?')[0])[1] or '.png'
                    fname = f"{prefix}_{safe_pid}{ext}"
                    urllib.request.urlretrieve(data_str, os.path.join(folder_name, fname))
                    return fname
                except: return data_str 
                    
            if data_str.startswith("data:image") or len(data_str) > 200:
                try:
                    b64_data = data_str.split("base64,")[1] if "base64," in data_str else data_str
                    fname = f"{prefix}_{safe_pid}.png"
                    with open(os.path.join(folder_name, fname), "wb") as f:
                        f.write(base64.b64decode(b64_data))
                    return fname
                except: pass
            
            if os.path.isfile(data_str):
                ext = os.path.splitext(data_str)[1] or '.png'
                fname = f"{prefix}_{safe_pid}{ext}"
                try:
                    shutil.copy(data_str, os.path.join(folder_name, fname))
                    return fname
                except: pass
                
            search_name = os.path.basename(data_str)
            for root_dir, dirs, files in os.walk('.'):
                if search_name in files:
                    found_path = os.path.join(root_dir, search_name)
                    ext = os.path.splitext(found_path)[1] or '.png'
                    fname = f"{prefix}_{safe_pid}{ext}"
                    try:
                        shutil.copy(found_path, os.path.join(folder_name, fname))
                        return fname
                    except: pass
        return ""

    for i, group in enumerate(groups):
        safe_group_id = f"group_{re.sub(r'[^a-zA-Z0-9]', '_', str(group))}"
        toc_lines.append(f"<li><a href='#{safe_group_id}'>{group} Family Group</a><ul>")

        self.db.cursor.execute("SELECT family_name, ancestral_family_name FROM families WHERE family_group = ?", (group,))
        records = list(set(self.db.cursor.fetchall()))

        G = nx.DiGraph()
        for f_name, a_name in records:
            parent = a_name if a_name and a_name.strip() else group
            if parent != f_name: G.add_edge(parent, f_name)
            else: G.add_node(f_name)

        # --- v19.32 BRANCH SUBGRAPH ISOLATION ---
        # If the user specifically requested a branch, mathematically isolate it!
        if target_family and target_family in G.nodes():
            descendants = nx.descendants(G, target_family)
            keep_nodes = descendants.union({target_family})
            G = G.subgraph(keep_nodes).copy()
            # Filter the master records list so the output tables only show this branch
            records = [(f, a) for f, a in records if f in keep_nodes]
        # ----------------------------------------
            
        img_filename = None
        if len(G.nodes()) > 0:
            try:
                max_people = 0
                for node in G.nodes():
                    try:
                        if anc_col:
                            q = f"SELECT COUNT(*) FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?)) OR LOWER(TRIM({anc_col})) = LOWER(TRIM(?))"
                            self.db.cursor.execute(q, (node, node))
                        else:
                            q = f"SELECT COUNT(*) FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?))"
                            self.db.cursor.execute(q, (node,))
                        count = self.db.cursor.fetchone()[0]
                        if count > max_people: max_people = count
                    except Exception: pass
                    
                vertical_gap = max(2.5, (max_people * 0.4) + 1.5)
                horizontal_gap = 5.0

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

                for root in sorted(roots): place_node(root)
                for node in G.nodes():
                    if node not in visited: place_node(node)

                y_vals = [p[1] for p in pos.values()]
                x_vals = [p[0] for p in pos.values()]
                y_range = abs(max(y_vals) - min(y_vals)) if y_vals else 10
                x_range = abs(max(x_vals) - min(x_vals)) if x_vals else 10
                
                ideal_w = max(12.0, (x_range * 0.5) + 4.0)
                ideal_h = max(9.0, (y_range * 0.4) + 4.0)
                
                plt.figure(figsize=(ideal_w, ideal_h))
                plt.title(f"Lineage Network: {group}", fontsize=14, fontweight='bold')
                
                node_colors = ['#2ecc71' if n == group or G.in_degree(n) == 0 else '#e74c3c' if G.out_degree(n) == 0 else '#3498db' for n in G.nodes()]
                nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=2500, edgecolors='black', linewidths=1.5)
                nx.draw_networkx_edges(G, pos, edgelist=G.edges(), edge_color='gray', width=2, alpha=0.7, arrows=True, arrowsize=20)
                nx.draw_networkx_labels(G, pos, font_size=9, font_weight='bold', bbox=dict(facecolor='white', edgecolor='black', boxstyle='round,pad=0.3', alpha=0.85))

                for node in G.nodes():
                    try:
                        if anc_col:
                            q = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?)) OR LOWER(TRIM({anc_col})) = LOWER(TRIM(?))"
                            self.db.cursor.execute(q, (node, node))
                        else:
                            q = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?))"
                            self.db.cursor.execute(q, (node,))
                            
                        fuzzy_matches = self.db.cursor.fetchall()
                        if not fuzzy_matches: continue
                            
                        x, y = pos[node]
                        text_y = y - 0.45  
                        
                        for row in fuzzy_matches:
                            row_dict = dict(zip(columns, row))
                            raw_name = row_dict.get(name_col, '')
                            p_name = f"<Blank Name, ID: {row_dict.get(id_col, '0')}>" if not raw_name or not str(raw_name).strip() else str(raw_name).strip()
                                
                            p_color = 'black' 
                            is_departed = False
                            is_moved = False

                            if departed_col and str(row_dict.get(departed_col, '')).strip().lower() in ['1', 'true', 'yes', 'y']: is_departed = True
                            if moved_col and str(row_dict.get(moved_col, '')).strip().lower() in ['1', 'true', 'yes', 'y']: is_moved = True
                            if status_col:
                                val = str(row_dict.get(status_col, '')).lower()
                                if 'departed' in val or 'deceased' in val or 'dead' in val: is_departed = True
                                elif 'moved' in val: is_moved = True

                            current_fam = str(row_dict.get(fam_col, '')).strip().lower()
                            if current_fam != str(node).strip().lower(): is_moved = True

                            if is_departed: p_color = 'red'
                            elif is_moved: p_color = 'gray'
                                
                            txt = plt.text(x, text_y, p_name, color=p_color, fontsize=9, fontweight='bold', ha='center', va='top', zorder=10)
                            txt.set_bbox(dict(facecolor='#f8f9fa', edgecolor='lightgray', alpha=0.95, boxstyle='round,pad=0.2'))
                            text_y -= 0.35 
                    except Exception: pass
        
                if y_vals and x_vals:
                    bottom_padding = (max_people * 0.4) + 2.0
                    plt.ylim(min(y_vals) - bottom_padding, max(y_vals) + 2)
                    plt.xlim(min(x_vals) - 2, max(x_vals) + horizontal_gap + 1.0)

                plt.axis('off')
                safe_filename = re.sub(r'[^a-zA-Z0-9]', '_', group)
                img_filename = f"graph_full_{safe_filename}.png"
                img_path = os.path.join(folder_name, img_filename)
                plt.tight_layout()
                plt.savefig(img_path, dpi=150, bbox_inches='tight')
                plt.close() 
            except Exception as e:
                plt.close()

        body_html.append(f"<h2 id='{safe_group_id}'>{group} Family Group</h2>")
        latex_content.append(rf"\section{{{escape_latex(group)}}}")

        if records:
            body_html.append("<h3>Families in this Group</h3>")
            body_html.append("<table><tr><th>Family Group</th><th>Family Name</th><th>Ancestral Family</th></tr>")
            
            latex_content.append(r"\subsection*{Families in this Group}")
            latex_content.append(r"\addcontentsline{toc}{subsection}{Families in this Group}")
            latex_content.append(r"\begin{longtable}{|p{0.25\textwidth}|p{0.35\textwidth}|p{0.3\textwidth}|}")
            latex_content.append(r"\hline")
            latex_content.append(r"\textbf{Family Group} & \textbf{Family Name} & \textbf{Ancestral Family} \\ \hline")
            latex_content.append(r"\endfirsthead")
            
            for f_name, a_name in records:
                safe_a = str(a_name).strip() if a_name else ""
                body_html.append(f"<tr><td>{group}</td><td>{str(f_name)}</td><td>{safe_a}</td></tr>")
                latex_content.append(f"{escape_latex(group)} & {escape_latex(str(f_name))} & {escape_latex(safe_a)} \\\\ \\hline")
                
            body_html.append("</table>")
            latex_content.append(r"\end{longtable}")
            latex_content.append(r"\vspace{0.5cm}")
        
        if img_filename:
            body_html.append(f'<div class="scroll-box"><img src="{img_filename}" alt="{group} Lineage Graph"></div>')
            latex_content.append(r"\begin{center}")
            latex_content.append(rf"\includegraphics[width=0.9\textwidth]{{{img_filename}}}")
            latex_content.append(rf"\\ \vspace{{0.2cm}} \textit{{Lineage graph for {escape_latex(group)}}}")
            latex_content.append(r"\end{center}")
            latex_content.append(r"\vspace{0.5cm}")

        sorted_nodes = list(nx.topological_sort(G)) if nx.is_directed_acyclic_graph(G) else list(G.nodes())
        
        for node in sorted_nodes:
            if anc_col:
                q = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?)) OR LOWER(TRIM({anc_col})) = LOWER(TRIM(?))"
                self.db.cursor.execute(q, (node, node))
            else:
                q = f"SELECT * FROM people WHERE LOWER(TRIM({fam_col})) = LOWER(TRIM(?))"
                self.db.cursor.execute(q, (node,))
                
            node_people = self.db.cursor.fetchall()
            if not node_people: continue
                
            safe_node_id = f"node_{safe_group_id}_{re.sub(r'[^a-zA-Z0-9]', '_', str(node))}"
            toc_lines.append(f"<li><a href='#{safe_node_id}'>Subfamily: {node}</a></li>")
            
            if not any(f['name'] == str(node) for f in global_families):
                global_families.append({'name': str(node), 'html_link': f"#{safe_node_id}", 'tex_link': f"family:{safe_node_id}"})
                
            body_html.append(f"<h3 id='{safe_node_id}'>Subfamily: {node}</h3>")
            latex_content.append(rf"\subsection{{Subfamily: {escape_latex(node)}}}")
            latex_content.append(rf"\label{{family:{safe_node_id}}}")
            
            if display_cols:
                body_html.append("<table><tr>")
                for c in display_cols:
                    header_name = "Age" if c == 'computed_age' else c.replace('_', ' ').title()
                    body_html.append(f"<th>{header_name}</th>")
                body_html.append("</tr>")

                col_width = round(0.95 / len(display_cols), 3)
                latex_table_def = r"\begin{longtable}{|" + "|".join([f"p{{{col_width}\\textwidth}}"] * len(display_cols)) + "|}"
                latex_content.append(latex_table_def)
                
                latex_content.append(r"\hline")
                tex_headers = ["Age" if c == 'computed_age' else c.replace('_', ' ').title() for c in display_cols]
                latex_content.append(" & ".join([r"\textbf{" + escape_latex(h) + "}" for h in tex_headers]) + r" \\ \hline")
                latex_content.append(r"\endfirsthead")

            for row in node_people:
                row_dict = dict(zip(columns, row))
                p_name = str(row_dict.get(name_col, 'Unknown'))
                p_id = str(row_dict.get(id_col, '0'))
                
                # Pre-calculate flags so Age calculation can verify if deceased
                is_departed = False
                is_moved = False

                if departed_col and str(row_dict.get(departed_col, '')).strip().lower() in ['1', 'true', 'yes', 'y']: is_departed = True
                if moved_col and str(row_dict.get(moved_col, '')).strip().lower() in ['1', 'true', 'yes', 'y']: is_moved = True
                if status_col:
                    val = str(row_dict.get(status_col, '')).lower()
                    if 'departed' in val or 'deceased' in val or 'dead' in val: is_departed = True
                    elif 'moved' in val: is_moved = True

                current_fam = str(row_dict.get(fam_col, '')).strip().lower()
                if current_fam != str(node).strip().lower(): is_moved = True

                # --- NEW AGE CALCULATION ENGINE ---
                row_dict['computed_age'] = calculate_age(row_dict, is_departed)
                
                # --- DYNAMIC RELATIONSHIP AUGMENTATION ENGINE ---
                f_id_clean = get_clean_id(row_dict.get('father_id', ''))
                m_id_clean = get_clean_id(row_dict.get('mother_id', ''))
                
                brothers, sisters, siblings = set(), set(), set()
                for parent_id in [f_id_clean, m_id_clean]:
                    if parent_id:
                        if 'sex' in columns:
                            self.db.cursor.execute("SELECT id, sex FROM people WHERE (father_id = ? OR mother_id = ?) AND id != ?", (parent_id, parent_id, p_id))
                        else:
                            self.db.cursor.execute("SELECT id, '' as sex FROM people WHERE (father_id = ? OR mother_id = ?) AND id != ?", (parent_id, parent_id, p_id))
                            
                        for r in self.db.cursor.fetchall():
                            s_val = str(r[1]).strip().upper() if r[1] else ""
                            if s_val in ['M', 'MALE']: brothers.add(str(r[0]))
                            elif s_val in ['F', 'FEMALE']: sisters.add(str(r[0]))
                            else: siblings.add(str(r[0]))
                            
                if brothers: row_dict['brothers'] = ", ".join(list(brothers))
                if sisters: row_dict['sisters'] = ", ".join(list(sisters))
                if siblings: row_dict['siblings'] = ", ".join(list(siblings))

                if 'sex' in columns:
                    self.db.cursor.execute("SELECT id, sex FROM people WHERE father_id = ? OR mother_id = ?", (p_id, p_id))
                else:
                    self.db.cursor.execute("SELECT id, '' as sex FROM people WHERE father_id = ? OR mother_id = ?", (p_id, p_id))
                    
                derived_sons, derived_daus, derived_children = [], [], []
                existing_sons = str(row_dict.get('son_ids', ''))
                existing_daus = str(row_dict.get('daughter_ids', ''))
                
                for r in self.db.cursor.fetchall():
                    c_id = str(r[0])
                    s_val = str(r[1]).strip().upper() if r[1] else ""
                    if c_id not in existing_sons and c_id not in existing_daus:
                        if s_val in ['M', 'MALE']: derived_sons.append(c_id)
                        elif s_val in ['F', 'FEMALE']: derived_daus.append(c_id)
                        else: derived_children.append(c_id)
                        
                if derived_sons: row_dict['derived_sons'] = ", ".join(derived_sons)
                if derived_daus: row_dict['derived_daughters'] = ", ".join(derived_daus)
                if derived_children: row_dict['derived_children'] = ", ".join(derived_children)
                # ------------------------------------------------
                
                safe_p_name = re.sub(r'[^a-zA-Z0-9]', '_', p_name)
                person_filename = f"person_{p_id}_{safe_p_name}.html"
                
                if not any(p['id'] == p_id for p in global_persons):
                    global_persons.append({'id': p_id, 'name': p_name, 'html_link': person_filename, 'tex_link': f"profile:{p_id}"})
                
                if p_id not in media_cache:
                    photo_file = extract_media(row_dict.get(photo_col), "photo", p_id, p_name) if photo_col else ""
                    
                    qr_file = ""
                    if has_qrcode:
                        safe_p_id_str = str(p_id).replace('_', '-') + "_" + safe_p_name
                        qr_fname = f"qr_{safe_p_id_str}.png"
                        qr_fpath = os.path.join(folder_name, qr_fname)
                        
                        vcard = f"BEGIN:VCARD\nVERSION:3.0\nFN:{p_name}\n"
                        if row_dict.get('phone'): vcard += f"TEL;TYPE=CELL:{row_dict.get('phone')}\n"
                        if row_dict.get('gmail'): vcard += f"EMAIL:{row_dict.get('gmail')}\n"
                        if row_dict.get('location'): vcard += f"ADR;TYPE=HOME:;;{row_dict.get('location')};;;;\n"
                        if row_dict.get('homepage'): vcard += f"URL:{row_dict.get('homepage')}\n"
                        vcard += "END:VCARD"
                        
                        try:
                            qr = qrcode.QRCode(version=1, box_size=4, border=1)
                            qr.add_data(vcard)
                            qr.make(fit=True)
                            img = qr.make_image(fill_color="black", back_color="white")
                            img.save(qr_fpath)
                            qr_file = qr_fname
                        except Exception: pass
                            
                    media_cache[p_id] = (photo_file, qr_file)
                else:
                    photo_file, qr_file = media_cache[p_id]
                
                note_text = f"Note: Ages displayed in this report are calculated dynamically relative to the report generation date ({display_time}). Ages are intentionally excluded for deceased individuals."

                if p_id not in generated_profiles:
                    generated_profiles.add(p_id)
                    person_filepath = os.path.join(folder_name, person_filename)
                    
                    person_html = [
                        f"<html><head><meta charset='utf-8'><title>{p_name} - Profile</title>",
                        "<style>body { font-family: Arial, sans-serif; margin: 40px; color: #333; background: #f9f9f9;} ",
                        "h1 { border-bottom: 2px solid #2980b9; padding-bottom: 10px; color: #2c3e50; } ",
                        "table { border-collapse: collapse; width: 60%; margin-top: 20px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); } ",
                        "th, td { border: 1px solid #bdc3c7; padding: 12px; text-align: left; } ",
                        "th { background-color: #ecf0f1; width: 35%; color: #2c3e50; } ",
                        ".btn { display: inline-block; margin-top: 25px; padding: 10px 15px; background: #2980b9; color: white; text-decoration: none; border-radius: 4px; font-weight: bold; } ",
                        ".btn:hover { background: #1c5980; }</style></head><body>",
                        f"<h1>Profile: {p_name}</h1>"
                    ]
                    
                    latex_profiles_content.append(rf"\phantomsection\label{{profile:{p_id}}}")
                    latex_profiles_content.append(rf"\subsection*{{{escape_latex(p_name)}}}")
                    
                    if photo_file or qr_file:
                        person_html.append("<div style='display: flex; gap: 20px; margin-bottom: 20px;'>")
                        latex_profiles_content.append(r"\begin{center}")
                        if photo_file:
                            person_html.append(f"<img src='{photo_file}' alt='Photo' style='max-height: 200px; border-radius: 8px; border: 1px solid #ccc;'>")
                            latex_profiles_content.append(rf"\includegraphics[height=4cm,keepaspectratio]{{{photo_file}}}")
                        if qr_file:
                            person_html.append(f"<img src='{qr_file}' alt='QR Code' style='max-height: 200px; border: 1px solid #ccc; border-radius: 4px;'>")
                            if photo_file: latex_profiles_content.append(r"\hspace{1cm}")
                            latex_profiles_content.append(rf"\includegraphics[height=4cm,keepaspectratio]{{{qr_file}}}")
                        person_html.append("</div>")
                        latex_profiles_content.append(r"\end{center}")
                        latex_profiles_content.append(r"\vspace{0.2cm}")

                    if profile_display_cols:
                        person_html.append("<table>")
                        latex_profiles_content.append(r"\begin{longtable}{|p{0.3\textwidth}|p{0.65\textwidth}|}")
                        latex_profiles_content.append(r"\hline")

                        for c in profile_display_cols:
                            raw_val = row_dict.get(c)
                            if raw_val is None: continue
                            val = str(raw_val).strip()
                            if not val or val.lower() in ['none', 'null']: continue
                            
                            is_fam_col = ('family' in c.lower() and (c.lower().endswith('id') or c.lower().endswith('ids')))
                            rel_keywords = ['father', 'mother', 'parent', 'spouse', 'husband', 'wife', 'partner', 'son', 'daughter', 'child', 'relation', 'sibling', 'derived_children']
                            is_person_col = (c.lower() != id_col.lower()) and (any(k in c.lower() for k in rel_keywords) or ((c.lower().endswith('id') or c.lower().endswith('ids')) and not is_fam_col))
                            
                            if is_fam_col or is_person_col:
                                target_map = family_id_map if is_fam_col else people_id_map
                                parts = [p.strip() for p in val.split(',')]
                                formatted_parts = []
                                for p in parts:
                                    if p in target_map:
                                        formatted_parts.append(f"({p}) {target_map[p]}")
                                    else:
                                        formatted_parts.append(p)
                                val = ", ".join(formatted_parts)
                            
                            c_title = "Age" if c == 'computed_age' else c.replace('_', ' ').title()
                            if c_title in ['Son Id', 'Daughter Id', 'Husband Id', 'Wife Id']:
                                c_title = c_title.replace(' Id', ' Ids')
                                
                            html_val = val.replace('\n', '<br>')
                            person_html.append(f"<tr><th>{c_title}</th><td>{html_val}</td></tr>")
                            latex_profiles_content.append(rf"\textbf{{{escape_latex(c_title)}}} & {escape_latex(val)} \\ \hline")
                            
                        person_html.append("</table>")
                        latex_profiles_content.append(r"\end{longtable}")
                        latex_profiles_content.append(r"\vspace{0.5cm}")
                        
                    person_html.append("<a href='report.html' class='btn'>&larr; Back to Main Report</a>")
                    person_html.append(f"<p style='font-size: 11px; color: #999; margin-top: 40px;'>{note_text}</p>")
                    person_html.append("</body></html>")
                    
                    with open(person_filepath, "w", encoding="utf-8") as pf:
                        pf.write("\n".join(person_html))

                tr_style = ""
                tex_color_prefix = ""
                tex_color_suffix = ""
                
                if is_departed:
                    tr_style = " style='color: #e74c3c; font-weight: bold;'" 
                    tex_color_prefix = r"\textcolor{red}{\textbf{"
                    tex_color_suffix = r"}}"
                elif is_moved:
                    tr_style = " style='color: #7f8c8d; font-style: italic;'" 
                    tex_color_prefix = r"\textcolor{gray}{\textit{"
                    tex_color_suffix = r"}}"
                
                if display_cols:
                    body_html.append(f"<tr{tr_style}>")
                    tex_row = []
                    
                    for c in display_cols:
                        cell_val = str(row_dict.get(c, ''))
                        
                        if c == name_col:
                            photo_html = f"<img src='{photo_file}' style='height:40px; width:40px; border-radius:50%; vertical-align:middle; margin-right:5px; object-fit:cover; border: 1px solid #ccc;'/>" if photo_file else ""
                            qr_html = f"<img src='{qr_file}' style='height:40px; width:40px; vertical-align:middle; margin-right:10px; border: 1px solid #ccc; border-radius:4px;'/>" if qr_file else ""
                            img_html = photo_html + qr_html
                            body_html.append(f"<td>{img_html}<strong><a href='{person_filename}' title='View Full Profile'>{cell_val}</a></strong></td>")
                            
                            tex_photo = rf"\raisebox{{-0.3\height}}{{\includegraphics[height=0.6cm,width=0.6cm,keepaspectratio]{{{photo_file}}}}}~" if photo_file else ""
                            tex_qr = rf"\raisebox{{-0.3\height}}{{\includegraphics[height=0.6cm,width=0.6cm,keepaspectratio]{{{qr_file}}}}}~" if qr_file else ""
                            tex_img = tex_photo + tex_qr
                            tex_row.append(f"{tex_img}{tex_color_prefix}\\hyperref[profile:{p_id}]{{{escape_latex(cell_val)}}}{tex_color_suffix}")
                        else:
                            html_cell_val = cell_val.replace('\n', '<br>')
                            body_html.append(f"<td>{html_cell_val}</td>")
                            tex_row.append(f"{tex_color_prefix}{escape_latex(cell_val)}{tex_color_suffix}")
                        
                    body_html.append("</tr>")
                    latex_content.append(" & ".join(tex_row) + r" \\ \hline")
            
            if display_cols:
                body_html.append("</table>")
                latex_content.append(r"\end{longtable}")
                latex_content.append(r"\vspace{0.5cm}")
                
        toc_lines.append("</ul></li>")

    global_families.sort(key=lambda x: x['name'].lower())
    global_persons.sort(key=lambda x: x['name'].lower())
    
    toc_lines.append("<li><a href='#global_index'><strong>Global Index</strong></a></li>")
    toc_lines.append("</ul></div>")

    body_html.append("<h2 id='global_index'>Global Index</h2>")
    
    body_html.append("<h3>Index of Families</h3>")
    body_html.append("<div style='column-count: 3; column-gap: 20px;'><ul>")
    for f in global_families:
        body_html.append(f"<li><a href='{f['html_link']}'>{f['name']}</a></li>")
    body_html.append("</ul></div>")
    
    body_html.append("<h3>Index of Persons</h3>")
    body_html.append("<div style='column-count: 3; column-gap: 20px;'><ul>")
    for p in global_persons:
        body_html.append(f"<li><a href='{p['html_link']}'>{p['name']} (ID: {p['id']})</a></li>")
    body_html.append("</ul></div>")
    
    body_html.append(f"<hr><p style='text-align: center; font-size: 12px; color: #7f8c8d; margin-top: 40px;'><em>{note_text}</em></p>")

    latex_profiles_content.append(r"\newpage")
    latex_profiles_content.append(r"\section{Global Index}")
    
    latex_profiles_content.append(r"\subsection*{Index of Families}")
    latex_profiles_content.append(r"\addcontentsline{toc}{subsection}{Index of Families}")
    latex_profiles_content.append(r"\begin{multicols}{2}")
    latex_profiles_content.append(r"\begin{itemize}")
    for f in global_families:
        latex_profiles_content.append(rf"\item \hyperref[{f['tex_link']}]{{{escape_latex(f['name'])}}}")
    latex_profiles_content.append(r"\end{itemize}")
    latex_profiles_content.append(r"\end{multicols}")
    latex_profiles_content.append(r"\vspace{0.5cm}")
    
    latex_profiles_content.append(r"\subsection*{Index of Persons}")
    latex_profiles_content.append(r"\addcontentsline{toc}{subsection}{Index of Persons}")
    latex_profiles_content.append(r"\begin{multicols}{2}")
    latex_profiles_content.append(r"\begin{itemize}")
    for p in global_persons:
        latex_profiles_content.append(rf"\item \hyperref[{p['tex_link']}]{{{escape_latex(p['name'])} (ID: {p['id']})}}")
    latex_profiles_content.append(r"\end{itemize}")
    latex_profiles_content.append(r"\end{multicols}")

    latex_profiles_content.append(r"\vspace{1cm}")
    latex_profiles_content.append(r"\begin{center}")
    latex_profiles_content.append(rf"\textit{{\small {escape_latex(note_text)}}}")
    latex_profiles_content.append(r"\end{center}")

    final_html = []
    final_html.extend(html_header)
    final_html.extend(toc_lines)
    final_html.extend(body_html)
    final_html.append("</body></html>")
    
    with open(os.path.join(folder_name, "report.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(final_html))

    if generated_profiles:
        latex_content.extend(latex_profiles_content)
        
    latex_content.append(r"\end{document}")
    with open(os.path.join(folder_name, "report.tex"), "w", encoding="utf-8") as f:
        f.write("\n".join(latex_content))

    print(f"\nSUCCESS: Report generated successfully in directory: {os.path.abspath(folder_name)}")
    print("IMPORTANT: To generate the Table of Contents in the PDF, you MUST run 'pdflatex report.tex' TWICE in a row!")
    
