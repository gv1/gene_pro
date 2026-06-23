IMPORTANT:
    Most of this script is generated using Google Gemini

From The Devil's Dictionary (1881-1906) [devil]:

  GENEALOGY, n.  An account of one's descent from an ancestor who did
  not particularly care to trace his own.



start gui:
python genealogy.py -e sqlite3 -g 
python genealogy.py -e sqlite3 -g -d family_new.db

report generation from command line:
python genealogy.py -c --report --group "All Families" -r -d family_new.db

Creates report.html and related files in Fami* directory
to generate pdf: ( run pdflatex twice )
cd Famil*; 
pdflatex report.tex ; 
pdflatex report.tex



usage: genealogy.py [-h] [-p PASSWORD] [-e {sqlite3,sqlcipher}] -d DB
                    [-i INPUT] [-o OUTPUT]
                    [-f {csv,gedcom,json,jpg,jpeg,png,tex}] [-r] [--report]
                    [--group GROUP] [--branch BRANCH]
                    [--export-graph {graphml,gexf,gml}]
                    [--relationship RELATIONSHIP] [-c | -g]
                    [--import-file IMPORT_FILE] [--export-data EXPORT_DATA]

Genealogy Data Utility

options:
  -h, --help            show this help message and exit
  -p, --password PASSWORD
                        Password for SQLCipher database
  -e, --engine {sqlite3,sqlcipher}
                        Engine: sqlite3 (default) or sqlcipher
  -d, --db DB           Path to database file
  -i, --input INPUT     Input file path (Legacy)
  -o, --output OUTPUT   Output file path (Legacy)
  -f, --format {csv,gedcom,json,jpg,jpeg,png,tex}
                        File format for export or graphing
  -r, --readonly        Boot up database structure in safe Read-Only mode
  --report              Generate a family report
  --group GROUP         Specify a target Family Group
  --branch BRANCH       Specify a target Sub-Branch
  --export-graph {graphml,gexf,gml}
                        Export a network graph for a group
  --relationship RELATIONSHIP
                        Calculate relationship. Usage: id1,id2
  -c, --cli             Command line only mode
  -g, --gui             Graphical user interface (default)
  --import-file IMPORT_FILE
                        Import data. Usage: file.ged, .json, .vcf, .sql, or
                        'basepath' for CSVs
  --export-data EXPORT_DATA
                        Export data. Usage: file.ged, .json, .vcf, .sql, or
                        'basepath' for CSVs
