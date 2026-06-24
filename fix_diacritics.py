#!/usr/bin/env python3
"""
Conservative diacritics fixer for Romanian LaTeX thesis.
Only applies replacements where context makes the correct form unambiguous.
Skips code environments and protected LaTeX commands.
"""
import re

def protect_code_regions(line):
    """Replace code regions with unique placeholders to avoid touching them."""
    placeholders = {}
    counter = [0]
    def ph(m):
        key = f'XPLACEHOLDERX{counter[0]}X'
        placeholders[key] = m.group(0)
        counter[0] += 1
        return key
    line = re.sub(r'\\texttt\{[^}]*\}', ph, line)
    line = re.sub(r'\\verb\|[^|]*\|', ph, line)
    line = re.sub(r'\\verb![^!]*!', ph, line)
    line = re.sub(r'\\ac\{[^}]*\}', ph, line)
    line = re.sub(r'\\cite\{[^}]*\}', ph, line)
    line = re.sub(r'\\label\{[^}]*\}', ph, line)
    line = re.sub(r'\\ref\{[^}]*\}', ph, line)
    line = re.sub(r'\\url\{[^}]*\}', ph, line)
    line = re.sub(r'\$[^$]+\$', ph, line)
    return line, placeholders

def restore(line, placeholders):
    for key, val in placeholders.items():
        line = line.replace(key, val)
    return line

def fix(line):
    line, ph = protect_code_regions(line)

    # =========================================================
    # PREPOSITIONS / PARTICLES - always safe (standalone words)
    # =========================================================
    line = re.sub(r'\bcatre\b', 'către', line)
    line = re.sub(r'\bCatre\b', 'Către', line)
    line = re.sub(r'\bdupa\b', 'după', line)
    line = re.sub(r'\bDupa\b', 'După', line)
    line = re.sub(r'\binainte\b', 'înainte', line)
    line = re.sub(r'\binsa\b', 'însă', line)
    line = re.sub(r'\bInsa\b', 'Însă', line)
    line = re.sub(r'\binca\b', 'încă', line)
    line = re.sub(r'\bInca\b', 'Încă', line)
    line = re.sub(r'\bintre\b', 'între', line)
    line = re.sub(r'\bIntre\b', 'Între', line)
    line = re.sub(r'\bpana\b', 'până', line)
    line = re.sub(r'\bPana\b', 'Până', line)
    line = re.sub(r'\bfara\b', 'fără', line)
    line = re.sub(r'\bFara\b', 'Fără', line)
    line = re.sub(r'\bintotdeauna\b', 'întotdeauna', line)

    # =========================================================
    # FISIER FAMILY - always safe
    # =========================================================
    line = re.sub(r'\bfisierele\b', 'fișierele', line)
    line = re.sub(r'\bfisierului\b', 'fișierului', line)
    line = re.sub(r'\bfisierelor\b', 'fișierelor', line)
    line = re.sub(r'\bfisiere\b', 'fișiere', line)
    line = re.sub(r'\bfisier\b', 'fișier', line)
    line = re.sub(r'\bFisiere\b', 'Fișiere', line)
    line = re.sub(r'\bFisier\b', 'Fișier', line)

    # =========================================================
    # QUESTION / ANSWER - always safe
    # =========================================================
    line = re.sub(r'\bintrebare\b', 'întrebare', line)
    line = re.sub(r'\bintrebari\b', 'întrebări', line)
    line = re.sub(r'\bintrebările\b', 'întrebările', line)
    line = re.sub(r'\braspuns\b', 'răspuns', line)
    line = re.sub(r'\bRaspuns\b', 'Răspuns', line)
    line = re.sub(r'\braspunsuri\b', 'răspunsuri', line)
    line = re.sub(r'\braspunsurile\b', 'răspunsurile', line)
    line = re.sub(r'\braspunsului\b', 'răspunsului', line)

    # =========================================================
    # ABSTRACT NOUNS - unambiguous (no verb/definite confusion)
    # =========================================================
    line = re.sub(r'\btransparenta\b', 'transparență', line)
    line = re.sub(r'\bTransparenta\b', 'Transparență', line)
    line = re.sub(r'\bperformantei\b', 'performanței', line)
    line = re.sub(r'\bperformanta\b', 'performanță', line)
    line = re.sub(r'\bPerformanta\b', 'Performanța', line)
    line = re.sub(r'\bimportantei\b', 'importanței', line)
    line = re.sub(r'\bimportanta\b', 'importanță', line)
    line = re.sub(r'\beficientei\b', 'eficienței', line)
    line = re.sub(r'\beficienta\b', 'eficiență', line)
    line = re.sub(r'\bpersistenta\b', 'persistență', line)
    line = re.sub(r'\blatentei\b', 'latenței', line)
    line = re.sub(r'\blatenta\b', 'latență', line)
    line = re.sub(r'\bcompetentele\b', 'competențele', line)
    line = re.sub(r'\bcompetentelor\b', 'competențelor', line)
    line = re.sub(r'\bcompetente\b', 'competențe', line)
    line = re.sub(r'\bcompetenta\b', 'competență', line)
    line = re.sub(r'\bexperienta\b', 'experiență', line)
    line = re.sub(r'\brelevantei\b', 'relevanței', line)
    line = re.sub(r'\brelevanta\b', 'relevanță', line)
    line = re.sub(r'\bconcurentei\b', 'concurenței', line)
    line = re.sub(r'\bconcurenta\b', 'concurență', line)
    line = re.sub(r'\bdiscrepantei\b', 'discrepanței', line)
    line = re.sub(r'\bdiscrepanta\b', 'discrepanță', line)
    line = re.sub(r'\bInfluenta\b', 'Influența', line)
    line = re.sub(r'\binfluenta\b', 'influența', line)
    line = re.sub(r'\basistenta\b', 'asistență', line)
    line = re.sub(r'\binconsistenta\b', 'inconsistență', line)
    line = re.sub(r'\bconsistenta\b', 'consistența', line)
    line = re.sub(r'\bamenintari\b', 'amenințări', line)
    line = re.sub(r'\bamenintare\b', 'amenințare', line)
    line = re.sub(r'\breferintele\b', 'referințele', line)
    line = re.sub(r'\breferintei\b', 'referinței', line)
    line = re.sub(r'\breferinta\b', 'referință', line)
    line = re.sub(r'\bAudienta\b', 'Audiență', line)
    line = re.sub(r'\baudienta\b', 'audiență', line)
    line = re.sub(r'\binferenta\b', 'inferență', line)
    line = re.sub(r'\binferenta\b', 'inferența', line)
    line = re.sub(r'\binferen[tț]ei\b', 'inferenței', line)
    line = re.sub(r'\bconsistentei\b', 'consistenței', line)

    # =========================================================
    # ACTIONS / SOLUTIONS / FUNCTIONS / SECTIONS - always safe
    # =========================================================
    line = re.sub(r'\bactiunile\b', 'acțiunile', line)
    line = re.sub(r'\bactiunilor\b', 'acțiunilor', line)
    line = re.sub(r'\bactiunea\b', 'acțiunea', line)
    line = re.sub(r'\bactiuni\b', 'acțiuni', line)
    line = re.sub(r'\bactiune\b', 'acțiune', line)

    line = re.sub(r'\bsolutiile\b', 'soluțiile', line)
    line = re.sub(r'\bsolutiei\b', 'soluției', line)
    line = re.sub(r'\bsolutia\b', 'soluția', line)
    line = re.sub(r'\bsolutii\b', 'soluții', line)
    line = re.sub(r'\bsolutie\b', 'soluție', line)

    line = re.sub(r'\bFunctionalitatile\b', 'Funcționalitățile', line)
    line = re.sub(r'\bfunctionalitatile\b', 'funcționalitățile', line)
    line = re.sub(r'\bFunctionalitati\b', 'Funcționalități', line)
    line = re.sub(r'\bfunctionalitati\b', 'funcționalități', line)
    line = re.sub(r'\bfunctionalitate\b', 'funcționalitate', line)
    line = re.sub(r'\bFunctie\b', 'Funcție', line)
    line = re.sub(r'\bFunctii\b', 'Funcții', line)
    line = re.sub(r'\bfunctiei\b', 'funcției', line)
    line = re.sub(r'\bfunctia\b', 'funcția', line)
    line = re.sub(r'\bfunctii\b', 'funcții', line)
    line = re.sub(r'\bfunctie\b', 'funcție', line)

    line = re.sub(r'\bsectiunile\b', 'secțiunile', line)
    line = re.sub(r'\bsectiunii\b', 'secțiunii', line)
    line = re.sub(r'\bsectiuni\b', 'secțiuni', line)
    line = re.sub(r'\bsectiune\b', 'secțiune', line)

    line = re.sub(r'\bspecificatiile\b', 'specificațiile', line)
    line = re.sub(r'\bspecificatiei\b', 'specificației', line)
    line = re.sub(r'\bspecificatia\b', 'specificația', line)
    line = re.sub(r'\bspecificatii\b', 'specificații', line)
    line = re.sub(r'\bspecificatie\b', 'specificație', line)

    # =========================================================
    # INTERFACE / EXECUTION / CONVERSATION - always safe
    # =========================================================
    line = re.sub(r'\binterfetelor\b', 'interfețelor', line)
    line = re.sub(r'\binterfetei\b', 'interfeței', line)
    line = re.sub(r'\binterfete\b', 'interfețe', line)
    line = re.sub(r'\binterfata\b', 'interfață', line)
    line = re.sub(r'\bInterfata\b', 'Interfață', line)

    line = re.sub(r'\bexecutiei\b', 'execuției', line)
    line = re.sub(r'\bexecutia\b', 'execuția', line)
    line = re.sub(r'\bexecutii\b', 'execuții', line)
    line = re.sub(r'\bexecutie\b', 'execuție', line)

    line = re.sub(r'\bconversatiile\b', 'conversațiile', line)
    line = re.sub(r'\bconversatiei\b', 'conversației', line)
    line = re.sub(r'\bconversatia\b', 'conversația', line)
    line = re.sub(r'\bconversatii\b', 'conversații', line)
    line = re.sub(r'\bconversatie\b', 'conversație', line)

    line = re.sub(r'\binteractiunile\b', 'interacțiunile', line)
    line = re.sub(r'\binteractiunii\b', 'interacțiunii', line)
    line = re.sub(r'\binteractiunea\b', 'interacțiunea', line)
    line = re.sub(r'\binteractiuni\b', 'interacțiuni', line)
    line = re.sub(r'\binteractiune\b', 'interacțiune', line)

    # =========================================================
    # SEARCH / KNOWLEDGE / CONTRIBUTIONS / KEYBOARD
    # =========================================================
    line = re.sub(r'\bcunostiinte\b', 'cunoștințe', line)
    line = re.sub(r'\bcunostinte\b', 'cunoștințe', line)
    line = re.sub(r'\bcautare\b', 'căutare', line)
    line = re.sub(r'\bcautari\b', 'căutări', line)
    line = re.sub(r'\bcautat\b', 'căutat', line)
    line = re.sub(r'\bcautand\b', 'căutând', line)
    line = re.sub(r'\bContributia\b', 'Contribuția', line)
    line = re.sub(r'\bcontributia\b', 'contribuția', line)
    line = re.sub(r'\bContributii\b', 'Contribuții', line)
    line = re.sub(r'\bcontributii\b', 'contribuții', line)
    line = re.sub(r'\bContributiile\b', 'Contribuțiile', line)
    line = re.sub(r'\bcontributiile\b', 'contribuțiile', line)
    line = re.sub(r'\bStergere\b', 'Ștergere', line)
    line = re.sub(r'\bstergere\b', 'ștergere', line)
    line = re.sub(r'\btastatura\b', 'tastatură', line)
    line = re.sub(r'\biesirile\b', 'ieșirile', line)
    line = re.sub(r'\biesirilor\b', 'ieșirilor', line)
    line = re.sub(r'\biesirea\b', 'ieșirea', line)
    line = re.sub(r'\biesiri\b', 'ieșiri', line)
    line = re.sub(r'\biesire\b', 'ieșire', line)
    line = re.sub(r'\bintreprinse\b', 'întreprinse', line)
    line = re.sub(r'\bintrebuintari\b', 'întrebuințări', line)
    line = re.sub(r'\bintrebuintare\b', 'întrebuințare', line)
    line = re.sub(r'\bsfarsit\b', 'sfârșit', line)
    line = re.sub(r'\bSfarsit\b', 'Sfârșit', line)
    line = re.sub(r'\bsfarsite\b', 'sfârsite', line)

    # =========================================================
    # STATES / SETTINGS / PREFERENCES / CONDITIONS
    # =========================================================
    line = re.sub(r'\bstarile\b', 'stările', line)
    line = re.sub(r'\bstarilor\b', 'stărilor', line)
    line = re.sub(r'\bstari\b', 'stări', line)
    line = re.sub(r'\bsetarile\b', 'setările', line)
    line = re.sub(r'\bsetarilor\b', 'setărilor', line)
    line = re.sub(r'\bsetari\b', 'setări', line)
    line = re.sub(r'\bpreferintele\b', 'preferințele', line)
    line = re.sub(r'\bpreferintei\b', 'preferinței', line)
    line = re.sub(r'\bpreferinte\b', 'preferințe', line)
    line = re.sub(r'\bpreferinta\b', 'preferință', line)
    line = re.sub(r'\bconditionale\b', 'condiționale', line)
    line = re.sub(r'\bconditional\b', 'condițional', line)
    line = re.sub(r'\bconditionata\b', 'condiționată', line)
    line = re.sub(r'\bconditionat\b', 'condiționat', line)
    line = re.sub(r'\bconditionarii\b', 'condiționării', line)
    line = re.sub(r'\bcondititii\b', 'condiții', line)   # typo
    line = re.sub(r'\bconditii\b', 'condiții', line)
    line = re.sub(r'\bconditie\b', 'condiție', line)

    # =========================================================
    # NUMBERS / TOOLS / SEQUENCE / CELLS
    # =========================================================
    line = re.sub(r'\bunealta\b', 'unealtă', line)
    line = re.sub(r'\bnumarul\b', 'numărul', line)
    line = re.sub(r'\bnumarului\b', 'numărului', line)
    line = re.sub(r'\bNumarul\b', 'Numărul', line)
    line = re.sub(r'\bSecventa\b', 'Secvența', line)
    line = re.sub(r'\bsecventa\b', 'secvența', line)
    line = re.sub(r'\bsecventiala\b', 'secvențială', line)
    line = re.sub(r'\bsecventiale\b', 'secvențiale', line)
    line = re.sub(r'\bbinara\b', 'binară', line)
    line = re.sub(r'\bierarhica\b', 'ierarhică', line)
    line = re.sub(r'\bcelula\b', 'celulă', line)
    line = re.sub(r'\bintelegere\b', 'înțelegere', line)
    line = re.sub(r'\binteles\b', 'înțeles', line)
    line = re.sub(r'\badaugarea\b', 'adăugarea', line)
    line = re.sub(r'\badaugat\b', 'adăugat', line)

    # =========================================================
    # METRIC / SEMANTIC - as adjectives only (not definite nouns)
    # "metrica" after a noun = adjective → metrică
    # "semantica" after a noun = adjective → semantică
    # Pattern: preceded by "și", "o", "singură", "o singură", or after noun
    # Safe: use specific phrases identified from agents
    # =========================================================
    line = re.sub(r'\bprag de similaritate și metrica\b', 'prag de similaritate și metrică', line)
    line = re.sub(r'\bo singura? metrica\b', 'o singură metrică', line)
    line = re.sub(r'\bsimilaritatea semantica\b', 'similaritatea semantică', line)
    line = re.sub(r'\bpierdere semantica\b', 'pierdere semantică', line)
    line = re.sub(r'\bpartitionare semantica\b', 'partiționare semantică', line)

    # =========================================================
    # VERB FORMS - only truly unambiguous (not valid infinitives)
    # Rule: verbs replaced here are those that CANNOT be infinitives
    # or that always appear in 3rd-person indicative context
    # =========================================================
    # "exista" → "există": "a exista" is rare; overwhelmingly 3rd person
    line = re.sub(r'\bExista\b', 'Există', line)
    line = re.sub(r'\bexista\b', 'există', line)
    # "returneaza" - no ambiguity, specific conjugation
    line = re.sub(r'\breturneaza\b', 'returnează', line)
    # "creeaza" - same
    line = re.sub(r'\bcreeaza\b', 'creează', line)
    # "utilizeaza" - specific conjugation form
    line = re.sub(r'\butilizeaza\b', 'utilizează', line)
    # "captureaza" - same
    line = re.sub(r'\bcaptureaza\b', 'capturează', line)
    # "instruieste" - same
    line = re.sub(r'\binstruieste\b', 'instruiește', line)
    # "urmeaza" - used as 3rd person (what follows)
    line = re.sub(r'\burmeaza\b', 'urmează', line)
    # "intampina" - 3rd person
    line = re.sub(r'\bintampina\b', 'întâmpină', line)
    line = re.sub(r'\bîntampina\b', 'întâmpină', line)
    # Reflexive constructions - verb after "se" is always 3rd person indicative
    line = re.sub(r'\bse lupta\b', 'se luptă', line)
    line = re.sub(r'\bse elimina\b', 'se elimină', line)
    line = re.sub(r'\bse afla\b', 'se află', line)
    line = re.sub(r'\bse uita\b', 'se uită', line)
    line = re.sub(r'\bse adauga\b', 'se adaugă', line)
    line = re.sub(r'\bse observa\b', 'se observă', line)
    line = re.sub(r'\bse ridica\b', 'se ridică', line)
    line = re.sub(r'\bse datoreaza\b', 'se datorează', line)
    line = re.sub(r'\bse propaga\b', 'se propagă', line)
    # "necesita" after subject: 3rd person (to need)
    line = re.sub(r'\bnecesita\b', 'necesită', line)
    # "contine" - specific: almost never "a conține" in these contexts
    line = re.sub(r'\bcontine\b', 'conține', line)
    line = re.sub(r'\bContine\b', 'Conține', line)
    # 1st person plural - always safe (modal + "ăm" = different from infinitive)
    line = re.sub(r'\banalizam\b', 'analizăm', line)
    line = re.sub(r'\babordam\b', 'abordăm', line)
    line = re.sub(r'\blistam\b', 'listăm', line)
    line = re.sub(r'\bprezentam\b', 'prezentăm', line)
    line = re.sub(r'\btestam\b', 'testăm', line)
    line = re.sub(r'\brealizam\b', 'realizăm', line)
    line = re.sub(r'\bDerivam\b', 'Derivăm', line)
    line = re.sub(r'\bderivam\b', 'derivăm', line)
    line = re.sub(r'\bpreferam\b', 'preferăm', line)
    line = re.sub(r'\bbazam\b', 'bazăm', line)
    line = re.sub(r'\basiguram\b', 'asigurăm', line)

    # =========================================================
    # FEMININE ADJECTIVES - safe; Romanian feminine adj always ends in ă
    # (Definite forms of nouns that look the same are listed below
    # as SKIPPED with a comment)
    # =========================================================
    line = re.sub(r'\bagnostica\b', 'agnostică', line)
    line = re.sub(r'\biterativa\b', 'iterativă', line)
    line = re.sub(r'\bversatila\b', 'versatilă', line)
    line = re.sub(r'\babrupta\b', 'abruptă', line)
    line = re.sub(r'\bcomplexa\b', 'complexă', line)
    line = re.sub(r'\bdisponibila\b', 'disponibilă', line)
    line = re.sub(r'\binteractiva\b', 'interactivă', line)
    line = re.sub(r'\bfluida\b', 'fluidă', line)
    line = re.sub(r'\bprogresiva\b', 'progresivă', line)
    line = re.sub(r'\bizolata\b', 'izolată', line)
    line = re.sub(r'\breproductibila\b', 'reproductibilă', line)
    line = re.sub(r'\bintegrala\b', 'integrală', line)
    line = re.sub(r'\bcontextuala\b', 'contextuală', line)
    line = re.sub(r'\bredusa\b', 'redusă', line)
    line = re.sub(r'\bsporita\b', 'sporită', line)
    line = re.sub(r'\bdorita\b', 'dorită', line)
    line = re.sub(r'\bnecesara\b', 'necesară', line)
    line = re.sub(r'\bgresita\b', 'greșită', line)
    line = re.sub(r'\bgreșita\b', 'greșită', line)
    line = re.sub(r'\bineficienta\b', 'ineficientă', line)
    line = re.sub(r'\bmaxima\b', 'maximă', line)
    line = re.sub(r'\bgandita\b', 'gândită', line)
    line = re.sub(r'\bgândita\b', 'gândită', line)
    line = re.sub(r'\bextensibila\b', 'extensibilă', line)
    line = re.sub(r'\bcapabila\b', 'capabilă', line)
    line = re.sub(r'\baccesibila\b', 'accesibilă', line)
    line = re.sub(r'\buzuala\b', 'uzuală', line)
    line = re.sub(r'\bevidenta\b', 'evidentă', line)
    line = re.sub(r'\bfireasca\b', 'firească', line)
    line = re.sub(r'\bvizuala\b', 'vizuală', line)
    line = re.sub(r'\bnativa\b', 'nativă', line)
    line = re.sub(r'\bdirecta\b', 'directă', line)
    line = re.sub(r'\bautomata\b', 'automată', line)
    line = re.sub(r'\bexplicita\b', 'explicită', line)
    line = re.sub(r'\bimplicita\b', 'implicită', line)
    line = re.sub(r'\boptionala\b', 'opțională', line)
    line = re.sub(r'\bgratuita\b', 'gratuită', line)
    line = re.sub(r'\bpublica\b', 'publică', line)
    line = re.sub(r'\bproprietara\b', 'proprietară', line)
    line = re.sub(r'\binchisa\b', 'închisă', line)
    line = re.sub(r'\bvalida\b', 'validă', line)
    line = re.sub(r'\bfrecventa\b', 'frecventă', line)
    line = re.sub(r'\blunga\b', 'lungă', line)
    line = re.sub(r'\bsimpla\b', 'simplă', line)
    line = re.sub(r'\bestetica\b', 'estetică', line)
    line = re.sub(r'\badaptiva\b', 'adaptivă', line)
    line = re.sub(r'\bhibrida\b', 'hibridă', line)
    line = re.sub(r'\bfinala\b', 'finală', line)
    line = re.sub(r'\bFinala\b', 'Finală', line)
    line = re.sub(r'\bprincipal[aа]\b', 'principală', line)
    line = re.sub(r'\bavansata\b', 'avansată', line)
    line = re.sub(r'\btotala\b', 'totală', line)
    line = re.sub(r'\bumana\b', 'umană', line)
    line = re.sub(r'\bUmana\b', 'Umană', line)
    line = re.sub(r'\bminima\b', 'minimă', line)
    line = re.sub(r'\bpartajata\b', 'partajată', line)
    line = re.sub(r'\bincrementa(la)\b', 'incrementală', line)
    line = re.sub(r'\bcomercial[aа]\b', 'comercială', line)
    # SKIPPED (definite noun risk): "semantica", "dinamica", "statica",
    # "locala", "structura", "tehnica", "metoda", "alternativa"
    # These are handled manually below with specific safe phrases only:
    line = re.sub(r'\bsimilaritatea semantica\b', 'similaritatea semantică', line)
    line = re.sub(r'\bo tehnica\b', 'o tehnică', line)
    line = re.sub(r'\bO tehnica\b', 'O tehnică', line)
    line = re.sub(r'\bo metoda\b', 'o metodă', line)
    line = re.sub(r'\bO metoda\b', 'O metodă', line)
    line = re.sub(r'\bcea mai comuna\b', 'cea mai comună', line)
    line = re.sub(r'\bcea mai utila\b', 'cea mai utilă', line)
    line = re.sub(r'\bo alternativa\b', 'o alternativă', line)
    line = re.sub(r'\bO alternativa\b', 'O alternativă', line)
    line = re.sub(r'\bPlatforma enterprise\b', 'Platformă enterprise', line)

    # =========================================================
    # PAST PARTICIPLES (feminine) - unambiguous: "este/a fost Xată"
    # =========================================================
    line = re.sub(r'\bfacilitata\b', 'facilitată', line)
    line = re.sub(r'\butilizata\b', 'utilizată', line)
    line = re.sub(r'\bimplementata\b', 'implementată', line)
    line = re.sub(r'\bgenerata\b', 'generată', line)
    line = re.sub(r'\bevaluata\b', 'evaluată', line)
    line = re.sub(r'\bdefinita\b', 'definită', line)
    line = re.sub(r'\bpropusa\b', 'propusă', line)
    line = re.sub(r'\bdescrisa\b', 'descrisă', line)
    line = re.sub(r'\banalizata\b', 'analizată', line)
    line = re.sub(r'\bfolosita\b', 'folosită', line)
    line = re.sub(r'\bconstruita\b', 'construită', line)
    line = re.sub(r'\bintegrata\b', 'integrată', line)
    line = re.sub(r'\bobtinuta\b', 'obținută', line)
    line = re.sub(r'\brealizata\b', 'realizată', line)
    line = re.sub(r'\boptimizata\b', 'optimizată', line)
    line = re.sub(r'\bbazata\b', 'bazată', line)
    line = re.sub(r'\badaugata\b', 'adăugată', line)
    line = re.sub(r'\brezolvata\b', 'rezolvată', line)
    line = re.sub(r'\blivrata\b', 'livrată', line)
    line = re.sub(r'\bacoperita\b', 'acoperită', line)
    line = re.sub(r'\bsemnalata\b', 'semnalată', line)
    line = re.sub(r'\bmotivata\b', 'motivată', line)
    line = re.sub(r'\btratata\b', 'tratată', line)
    line = re.sub(r'\bdetaliata\b', 'detaliată', line)
    line = re.sub(r'\bgarantata\b', 'garantată', line)
    line = re.sub(r'\bdeterminata\b', 'determinată', line)
    line = re.sub(r'\bfructificata\b', 'fructificată', line)
    line = re.sub(r'\bpartitionata\b', 'partiționată', line)
    line = re.sub(r'\bpartitionare\b', 'partiționare', line)
    line = re.sub(r'\binregistrata\b', 'înregistrată', line)
    line = re.sub(r'\breprezentata\b', 'reprezentată', line)
    line = re.sub(r'\bsistematizata\b', 'sistematizată', line)
    line = re.sub(r'\bscurtcircuitata\b', 'scurtcircuitată', line)
    line = re.sub(r'\bsuprascrisa\b', 'suprascrisă', line)
    line = re.sub(r'\bformata\b', 'formată', line)
    line = re.sub(r'\bstocata\b', 'stocată', line)
    line = re.sub(r'\bsalvata\b', 'salvată', line)
    line = re.sub(r'\badoptata\b', 'adoptată', line)
    line = re.sub(r'\brecomandata\b', 'recomandată', line)
    line = re.sub(r'\bvalidata\b', 'validată', line)
    line = re.sub(r'\bnormalizata\b', 'normalizată', line)
    line = re.sub(r'\bclasificata\b', 'clasificată', line)
    line = re.sub(r'\bindicata\b', 'indicată', line)
    line = re.sub(r'\bselectata\b', 'selectată', line)
    line = re.sub(r'\bdezvoltata\b', 'dezvoltată', line)
    line = re.sub(r'\bconfigurata\b', 'configurată', line)
    line = re.sub(r'\bparcursa\b', 'parcursă', line)
    line = re.sub(r'\bpusa\b', 'pusă', line)
    line = re.sub(r'\bprezentata\b', 'prezentată', line)
    line = re.sub(r'\bpartajata\b', 'partajată', line)
    line = re.sub(r'\bsistematizata\b', 'sistematizată', line)
    line = re.sub(r'\bcompletata\b', 'completată', line)  # only "completată", not "completa"

    # =========================================================
    # SPECIFIC PHRASES - context verified by agents
    # =========================================================
    line = re.sub(r'\bData fiind\b', 'Dată fiind', line)
    line = re.sub(r'\bNota:', 'Notă:', line)
    line = re.sub(r'\bcod sursa\b', 'cod sursă', line)
    line = re.sub(r'\bcodul sursa\b', 'codul sursă', line)
    line = re.sub(r'\bCod sursa\b', 'Cod sursă', line)
    line = re.sub(r'\bde baza\b', 'de bază', line)
    line = re.sub(r'\bo baza\b', 'o bază', line)
    line = re.sub(r'\bîn fata\b', 'în față', line)
    line = re.sub(r'\bin fata\b', 'în față', line)
    line = re.sub(r'\bfata de\b', 'față de', line)
    line = re.sub(r'\bFata de\b', 'Față de', line)
    line = re.sub(r'\bdintr-o singura\b', 'dintr-o singură', line)
    line = re.sub(r'\bde ultima\b', 'de ultimă', line)
    line = re.sub(r'\bO ultima\b', 'O ultimă', line)
    line = re.sub(r'\bo ultima\b', 'o ultimă', line)
    line = re.sub(r'\bde lunga durata\b', 'de lungă durată', line)
    line = re.sub(r'\bo gama\b', 'o gamă', line)
    line = re.sub(r'\bo plaja\b', 'o plajă', line)
    line = re.sub(r'\bcota de piata\b', 'cotă de piață', line)
    line = re.sub(r'\bpiata\b', 'piață', line)
    line = re.sub(r'\bcota\b', 'cotă', line)
    line = re.sub(r'\bMenționăm ca\b', 'Menționăm că', line)
    line = re.sub(r'\bMentionam ca\b', 'Menționăm că', line)
    line = re.sub(r'\bconsider ca\b', 'consider că', line)
    line = re.sub(r'\bo diferenta\b', 'o diferență', line)
    line = re.sub(r'\bdoar diferenta\b', 'doar diferența', line)
    line = re.sub(r'\bdiferenta intre\b', 'diferența între', line)
    line = re.sub(r'\bdiferenta între\b', 'diferența între', line)
    line = re.sub(r'\bpropriu-zisa\b', 'propriu-zisă', line)
    line = re.sub(r'\bpropriu zisa\b', 'propriu-zisă', line)
    line = re.sub(r'\bmasina gazda\b', 'mașina gazdă', line)
    line = re.sub(r'\b/luna\b', '/lună', line)
    line = re.sub(r'\blibraria\b', 'librăria', line)
    line = re.sub(r'\bca pe testele\b', 'că pe testele', line)
    line = re.sub(r'\bca pipeline\b', 'că pipeline', line)
    line = re.sub(r'\bca modele\b', 'că modele', line)
    line = re.sub(r'\bca inginerul\b', 'că inginerul', line)
    line = re.sub(r'\bca agentul este\b', 'că agentul este', line)
    line = re.sub(r'\bca folosirea\b', 'că folosirea', line)
    line = re.sub(r'\bca modelul\b', 'că modelul', line)
    line = re.sub(r'\bca rezumatul\b', 'că rezumatul', line)
    line = re.sub(r'\bca cel mai\b', 'că cel mai', line)
    line = re.sub(r'\bca nu mai este\b', 'că nu mai este', line)
    line = re.sub(r'\bpentru ca\b', 'pentru că', line)
    line = re.sub(r'\bdin cauza ca\b', 'din cauza că', line)
    line = re.sub(r'\bîn vedere ca\b', 'în vedere că', line)
    line = re.sub(r'\bfaptul ca\b', 'faptul că', line)
    line = re.sub(r'\bîn cunoscuta de cauza\b', 'în cunoștință de cauză', line)
    line = re.sub(r'\bin cunostinta de cauza\b', 'în cunoștință de cauză', line)
    line = re.sub(r'\bdupa de cost\b', 'după cost', line)  # typo+diacritic
    line = re.sub(r'\bprezenta a cel putin\b', 'prezența a cel puțin', line)
    line = re.sub(r'\bprezenta a cel puțin\b', 'prezența a cel puțin', line)

    # =========================================================
    # TYPO FIXES
    # =========================================================
    line = re.sub(r'\bsufucientă\b', 'suficientă', line)
    line = re.sub(r'\bsufucient\b', 'suficient', line)
    line = re.sub(r'\bșaibe\b', 'aibă', line)
    line = re.sub(r'\bputem rezumă\b', 'putem rezuma', line)

    # =========================================================
    # "și" - replace " si " only when surrounded by word chars
    # Skip LaTeX command starts like \si
    # =========================================================
    # Replace standalone "si" not part of any word or LaTeX command
    line = re.sub(r'(?<=[^\w\\])si(?=\W)', 'și', line)
    # Revert any accidental \și → \si (LaTeX commands)
    line = line.replace('\\și', '\\si')

    line = restore(line, ph)
    return line


def main():
    path = '/Users/enanescu/stuff/lucrare/teza_v1.tex'
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    in_code_env = False
    for line in lines:
        stripped = line.rstrip('\n')
        if re.search(r'\\begin\{(lstlisting|verbatim|minted|Verbatim|lstinputlisting)\}', stripped):
            in_code_env = True
            new_lines.append(line)
            continue
        if re.search(r'\\end\{(lstlisting|verbatim|minted|Verbatim|lstinputlisting)\}', stripped):
            in_code_env = False
            new_lines.append(line)
            continue
        if in_code_env:
            new_lines.append(line)
            continue
        if stripped.lstrip().startswith('%'):
            new_lines.append(line)
            continue
        fixed = fix(stripped)
        new_lines.append(fixed + '\n' if line.endswith('\n') else fixed)

    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("Done.")


if __name__ == '__main__':
    main()
