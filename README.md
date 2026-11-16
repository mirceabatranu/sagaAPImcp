# saga-mcp

Server MCP care îi dă lui Claude (Claude Code / Claude Desktop) acces la contabilitatea din
**SAGA Web**, pentru una sau mai multe firme: balanță, solduri de cont, stocuri și import de
facturi, note contabile, încasări, plăți, clienți și articole.

Folosește [API-ul oficial SAGA Web](https://web.sagasoft.ro/DocumentatieAPI). Proiect
neoficial, fără legătură cu SAGA Soft.

## Ce poate face

| Unealtă | Ce face | Scrie în SAGA? |
|---|---|---|
| `saga_balanta` | Balanța sintetică sau analitică pe o perioadă, filtrabilă pe conturi | Nu |
| `saga_sold_cont` | Soldul unui cont la o dată (ex. 5311 casa, 4423 TVA de plată) | Nu |
| `saga_stoc_articol` | Stocul unui articol, opțional pe gestiune | Nu |
| `saga_situatie_stocuri` | Situația stocurilor pe o perioadă | Nu |
| `saga_articol` | Fișa unui articol | Nu |
| `saga_status` | Configurarea: adresa API, cheile salvate | Nu |
| `saga_pregateste_import` | Construiește fișierul XML de import (F, C, I, P, NC, CLI, ART) și îl salvează local | Nu |
| `saga_importa` | Trimite fișierul în SAGA, **în doi pași, cu confirmarea ta** | Da |

Resursa `saga://formate-import` conține structura fișierelor XML de import.

Exemple de întrebări pentru Claude:

- „Cât am în casă azi la firma 43424557?”
- „Arată-mi TVA-ul de plată din balanța pe octombrie pentru toți clienții din lista asta.”
- „Fă nota contabilă pentru bonurile de combustibil din tabelul ăsta și pregătește importul.”

## Siguranță

- **Citirile nu modifică nimic.**
- **Importul se face în doi pași.** Primul apel verifică fișierul și arată un rezumat (număr de
  documente, sume). Al doilea apel trimite fișierul doar cu tokenul din primul pas, legat de
  conținutul exact al fișierului.
- **Implicit, importul nu se finalizează singur.** Ajunge în SAGA la *Diverse → Import date*,
  unde îl verifici și îl finalizezi tu. Importul automat (`import_automat=true`) e blocat
  dacă nu pornești serverul cu `SAGA_ALLOW_AUTO_IMPORT=1`.
- **Cheia de acces** stă în depozitul securizat al sistemului (Windows Credential Manager),
  nu în fișiere de configurare.

## Instalare

### 1. Cerințe

- Python 3.10 sau mai nou: <https://www.python.org/downloads/>. La instalare bifează
  *Add python.exe to PATH*.
- Claude Code sau Claude Desktop.
- Un utilizator **Admin** în SAGA Web, ca să generezi cheia de acces.

### 2. Instalează serverul

În PowerShell:

```powershell
python -m pip install --user uv
python -m uv tool install git+https://github.com/mirceabatranu/sagaAPImcp
python -m uv tool update-shell
```

Închide și redeschide PowerShell, apoi verifică:

```powershell
saga-mcp --version
```

Pentru actualizare la o versiune nouă:

```powershell
python -m uv tool upgrade saga-mcp
```

### 3. Generează și salvează cheia de acces

1. În SAGA Web: **Utilizatori → Integrare API → Generează cheie de acces**.
2. În PowerShell:

   ```powershell
   saga-mcp set-key
   ```

   Lipește cheia (nu se afișează pe ecran) și apasă Enter.

Dacă unele firme sunt în alt cont SAGA Web, cu altă cheie, salvează cheia separat pentru
fiecare firmă:

```powershell
saga-mcp set-key --cif 12345678
```

Serverul folosește cheia firmei, dacă există, altfel cheia comună.

3. Testează conexiunea (citește soldul casei, 5311):

   ```powershell
   saga-mcp test --cif 43424557
   ```

### 4. Conectează la Claude

**Claude Code:**

```powershell
claude mcp add --scope user saga -- "$env:USERPROFILE\.local\bin\saga-mcp.exe"
claude mcp list
```

Trebuie să apară `saga: ... ✔ Connected`. Uneltele sunt disponibile în sesiunile noi.

**Claude Desktop:** *Settings → Developer → Edit Config*, adaugă în
`claude_desktop_config.json` (înlocuiește `NUME` cu utilizatorul Windows) și repornește
Claude Desktop:

```json
{
  "mcpServers": {
    "saga": {
      "command": "C:\\Users\\NUME\\.local\\bin\\saga-mcp.exe"
    }
  }
}
```

## Setări (variabile de mediu, opționale)

| Variabilă | Implicit | Rol |
|---|---|---|
| `SAGA_API_URL` | `https://web.sagasoft.ro/api/v20260225` | Adresa API (se schimbă când SAGA publică o versiune nouă) |
| `SAGA_DATA_DIR` | `Documents\SAGA` | Unde se salvează balanțele, stocurile și fișierele de import, pe firme |
| `SAGA_CIF` | – | Firma implicită, când nu se dă `cif` |
| `SAGA_ALLOW_AUTO_IMPORT` | – | `1` permite importul finalizat automat |
| `SAGA_MAX_ROWS` | `500` | Câte rânduri de balanță intră în răspuns (fișierul complet se salvează oricum) |

Exemplu în Claude Code, cu firma implicită:

```powershell
claude mcp add --scope user saga -e SAGA_CIF=43424557 -- "$env:USERPROFILE\.local\bin\saga-mcp.exe"
```

## Rotirea cheii

SAGA trimite uneori o cheie nouă în răspuns (headerul `X-Saga-Refresh-Token`) și o
blochează pe cea veche. Serverul salvează automat cheia nouă și face cererile pe rând,
ca o cerere să nu plece cu o cheie deja înlocuită.

Dacă totuși apare eroarea 401 (de exemplu, aceeași cheie a fost folosită și din altă
aplicație, care a rotit-o), generează o cheie nouă în SAGA Web și rulează din nou
`saga-mcp set-key`.

**Nu folosi aceeași cheie pe două calculatoare.** Când unul o rotește, celălalt rămâne
cu cheia blocată. Generează câte o cheie (sau câte un utilizator API) pentru fiecare
calculator.

## Folosit împreună cu ANAF

Merge bine cu [anafpy](https://github.com/robert-malai/anafpy) (server MCP pentru ANAF:
SPV, e-Factura, declarații). Exemple:

- TVA din balanța SAGA (4423 / 4424) comparat cu D300 depus (duplicat din SPV)
- facturi primite în e-Factura → fișier de import SAGA → finalizat de tine în SAGA
- soldurile 44x / 43x din SAGA comparate cu obligațiile de plată din SPV

## Dezvoltare

Testul rulează pe un SAGA simulat, fără rețea și fără cheie reală:

```powershell
git clone https://github.com/mirceabatranu/sagaAPImcp
cd sagaAPImcp
python -m uv run python tests/test_mock.py
```
