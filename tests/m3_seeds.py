"""Phase 3 milestone 3: curated seed questions (offline, deterministic).

The first ten entries (SHOWCASE) pin exact visual behaviour; the EXTRA
seeds widen subject/skip/negative coverage. Every size prefix starts
with SHOWCASE so small PDFs exercise every major visual type.
"""

from __future__ import annotations


def E(stem, opts, correct, expl="", tag="", track=None, expect="unset"):
    return {"stem": stem, "opts": list(opts), "correct": int(correct),
            "expl": expl, "tag": tag, "track": track, "expect": expect}


SHOWCASE = [
    E("Where is Chilika lake located?",
      ["Chilika Lake", "Sambhar Lake", "Wular Lake", "Dal Lake"], 0,
      "Chilika is a brackish lagoon located on the Odisha coast. "
      "It is a famous winter home for migratory birds.",
      tag="geo/map-en", expect="location_map"),
    E("सांभर झील कहाँ स्थित है?",
      ["Sambhar Lake", "Chilika Lake", "Wular Lake", "Dal Lake"], 0,
      "सांभर झील भारत के पश्चिम में स्थित एक खारे पानी की झील है। "
      "यह नमक उत्पादन के लिए प्रसिद्ध है।",
      tag="geo/map-hi", track="सांभर", expect="location_map"),
    E("Arrange the following events in chronological order.",
      ["1857, 1919, 1942", "1919, 1942, 1857", "1942, 1857, 1919",
       "1857, 1942, 1919"], 0,
      "Read the years in order. The revolt of 1857 shook the empire. "
      "In 1919 came a massacre. The Quit India movement followed in 1942.",
      tag="hist/timeline", expect="timeline"),
    E("List the steps to file an RTI application correctly.",
      ["Write, pay, submit, collect", "Pay, write, collect, submit",
       "Submit, pay, write, collect", "Collect, submit, pay, write"], 0,
      "Follow these points in order.\n"
      "1. Write the application.\n"
      "2. Pay the fee.\n"
      "3. Submit to the officer.\n"
      "4. Collect the reply.",
      tag="polity/flowchart", expect="flowchart"),
    E("Describe the process of urine formation in detail.",
      ["Filtration to excretion", "Excretion to filtration",
       "Only filtration", "Only excretion"], 0,
      "The nephron works in stages.\n"
      "1. Filtration occurs first.\n"
      "2. Reabsorption follows next.\n"
      "3. Secretion happens after.\n"
      "4. Excretion completes it.",
      tag="sci/process", expect="process"),
    E("What is the difference between Lok Sabha and Rajya Sabha?",
      ["Direct vs indirect election", "Same election mode",
       "Both nominated", "Both hereditary"], 0,
      "Both Houses differ in composition.\n"
      "• Lok Sabha members are directly elected.\n"
      "• Rajya Sabha members are indirectly elected.\n"
      "• Both form the Parliament of India.",
      tag="polity/comparison", expect="comparison"),
    E("What are the causes and effects of deforestation in detail?",
      ["Mining erodes soil and climate", "Planting erodes soil",
       "Rains stop fully", "Nothing changes"], 0,
      "Forests shape land and air.\n"
      "Causes:\n"
      "• Cutting of trees\n"
      "• Mining\n"
      "Effects:\n"
      "• Soil erosion\n"
      "• Climate change",
      tag="env/cause", expect="cause_effect"),
    E("Explain the water cycle with its stages in detail.",
      ["Evaporation to collection", "Collection to evaporation only",
       "Only rainfall", "Only rivers"], 0,
      "The cycle repeats continuously.\n"
      "Stages:\n"
      "Evaporation, Condensation, Precipitation, Collection",
      tag="geo/cycle", expect="cycle"),
    E("What are the three types of rocks? Explain each briefly.",
      ["Igneous, sedimentary, metamorphic", "Only igneous",
       "Only sand", "Only clay"], 0,
      "Rocks fall into three families.\n"
      "• Igneous rocks form from magma.\n"
      "• Sedimentary rocks form in layers.\n"
      "• Metamorphic rocks transform under heat.",
      tag="geo/classification", expect="classification"),
    E("What is 7 x 8?",
      ["54", "56", "58", "60"], 1,
      "It equals 56.",
      tag="none/arithmetic", expect=None),
]

EXTRA = [
    E("What does the Indian Constitution comprise? List the parts.",
      ["Preamble to Schedules", "Only the Preamble",
       "Only Schedules", "Only courts"], 0,
      "Read the parts below. The Indian Constitution comprises the "
      "Preamble, Fundamental Rights, Directive Principles and Schedules "
      "for the welfare of all citizens.",
      tag="polity/concept"),
    E("Write short notes on the following provisions in detail.",
      ["All six provisions", "Only one provision",
       "No provision", "Only courts"], 0,
      "Revise each point carefully.\n"
      "• Provision one ensures basic safeguards for all citizens in "
      "every state of India.\n"
      "• Provision two lays down directive goals for the welfare "
      "state to follow.\n"
      "• Provision three describes emergency powers with proper "
      "parliamentary checks.\n"
      "• Provision four covers amendment procedures and their "
      "constitutional limits.\n"
      "• Provision five lists schedules, subjects and lists of the "
      "federation.\n"
      "• Provision six explains tribunals, services and special "
      "provisions clearly.",
      tag="polity/infographic"),
    E("Classify the following soils with examples in detail.",
      ["Alluvial, Black, Red", "Only alluvial",
       "Only black", "Only desert sand"], 0,
      "Match each soil with its region.\n"
      "• Alluvial: northern plains, delta tracts\n"
      "• Black: volcanic plateau, cotton belt\n"
      "• Red: crystalline uplands, southern plateau",
      tag="geo/class-groups"),
    E("Name the parts of the human heart for a labelled sketch.",
      ["Four chambers and valves", "Only one chamber",
       "Only veins", "Only arteries"], 0,
      "Learn the parts in order.\n"
      "• Right atrium receives used blood.\n"
      "• Right ventricle pumps to the lungs.\n"
      "• Left atrium receives fresh blood.\n"
      "• Left ventricle pumps to the body.",
      tag="sci/heart"),
    E("Consider the following statements:\n"
      "1. The Constitution is the supreme law of the land.\n"
      "2. Fundamental Rights are enforceable by the courts.",
      ["1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2"], 2,
      "Both statements are correct descriptions of the system.",
      tag="polity/statements-a", track="supreme law"),
    E("Consider the following statements:\n"
      "1. The monsoon brings most of the annual rainfall.\n"
      "2. The retreating monsoon waters the south-east coast.\n"
      "3. El Nino can weaken the monsoon rains.",
      ["1 only", "1 and 2 only", "1, 2 and 3", "2 and 3 only"], 2,
      "All three statements correctly describe the monsoon system.",
      tag="geo/statements-b", track="monsoon brings"),
    E("Where is the city of Xyzabc located?",
      ["Xyzabc", "Nowhere", "Unknown", "None"], 0,
      "No such city exists in our dataset records.",
      tag="skip/unknown", expect=None),
    E("Where is London?",
      ["London", "Paris", "Rome", "Madrid"], 0,
      "London is the capital of the UK.",
      tag="skip/world", expect=None),
    E("Where is Paris situated?",
      ["Paris", "Lyon", "Nice", "Rome"], 0,
      "Paris is the capital of France and lies outside our map data.",
      tag="skip/world2", expect=None),
    E("What is 144 divided by 12?",
      ["10", "11", "12", "14"], 2,
      "Twelve times twelve equals one forty-four.",
      tag="none/arithmetic2", expect=None),
    E("Where is Chilika lake?",
      ["Chilika Lake", "Sambhar Lake", "Wular Lake", "Dal Lake"], 0,
      "",
      tag="skip/no-expl-map"),
    E("What is 9 x 9?",
      ["79", "81", "89", "91"], 1,
      "",
      tag="skip/no-expl-none", expect=None),
    E("भारत का राष्ट्रीय पशु कौन सा है?",
      ["Tiger", "Lion", "Elephant", "Bear"], 0,
      "बाघ भारत का राष्ट्रीय पशु है। यह शक्ति का प्रतीक है।",
      tag="none/hindi", track="राष्ट्रीय पशु", expect=None),
    E("The Quit India movement demanded what? भारत छोड़ो का नारा क्या था?",
      ["British must leave India", "More councils",
       "Separate electorates", "Dominion status"], 0,
      "The call was simple: the British must leave India at once. "
      "अंग्रेजों भारत छोड़ो।",
      tag="hist/hinglish", track="demanded what"),
    E("How is the Constitution amended? Outline the procedure briefly.",
      ["Bill, passage, assent", "Only assent",
       "Only passage", "No bill needed"], 0,
      "Follow the amendment procedure in order.\n"
      "1. A bill is introduced in Parliament.\n"
      "2. Both Houses pass it by majority.\n"
      "3. The President gives assent to the bill.",
      tag="polity/amend-flow"),
    E("How is the annual budget passed? List the stages.",
      ["Proposal, debate, vote, assent", "Only debate",
       "Only vote", "No proposal"], 0,
      "The budget follows a fixed procedure.\n"
      "1. The budget is proposed in the House.\n"
      "2. Members debate the proposals.\n"
      "3. Demands are voted in the House.\n"
      "4. The bill gets assent and becomes law.",
      tag="econ/budget-flow"),
    E("What is the difference between fiscal policy and monetary policy?",
      ["Budget vs money supply", "Same tools",
       "Only taxes", "Only banks"], 0,
      "Both policies steer the economy.\n"
      "• Fiscal policy uses taxes and spending.\n"
      "• Monetary policy uses rates and money supply.\n"
      "• Both aim at stable growth for all.",
      tag="econ/comparison"),
    E("What are the causes and effects of inflation in detail?",
      ["Demand pushes prices up", "Supply lowers prices",
       "Money gains value", "Nothing changes"], 0,
      "Prices rise for clear reasons.\n"
      "Causes:\n"
      "• Excess demand in markets\n"
      "• Rising fuel costs\n"
      "Effects:\n"
      "• Savings lose real value\n"
      "• Poor suffer the most",
      tag="econ/cause"),
    E("Explain the nitrogen cycle with its stages in detail.",
      ["Fixation to denitrification", "Only fixation",
       "Only plants", "Only rain"], 0,
      "Nitrogen moves through stages.\n"
      "Stages:\n"
      "Fixation, Nitrification, Assimilation, Denitrification",
      tag="env/cycle"),
    E("Describe the process of photosynthesis in green plants.",
      ["Light to glucose", "Glucose to light",
       "Only water", "Only soil"], 0,
      "Leaves run a food factory.\n"
      "1. Leaves trap sunlight energy.\n"
      "2. Roots absorb water from soil.\n"
      "3. Air gives carbon dioxide gas.\n"
      "4. Glucose is formed in leaves.",
      tag="sci/process2"),
    E("What are the three types of rivers by flow? Explain each briefly.",
      ["Perennial, seasonal, ephemeral", "Only perennial",
       "Only seasonal", "Only canals"], 0,
      "Rivers differ by water flow.\n"
      "• Perennial rivers flow all year.\n"
      "• Seasonal rivers flow in rains.\n"
      "• Ephemeral rivers flow after storms.",
      tag="geo/rivers-class"),
    E("The Preamble of India promises what to citizens?",
      ["Justice, liberty, equality", "Only duties",
       "Only taxes", "Only posts"], 0,
      "Read the promise below. The Preamble comprises justice, liberty, "
      "equality and fraternity for the dignity of all citizens.",
      tag="polity/preamble"),
    E("Which Article guarantees equality before law?",
      ["Article 14", "Article 19", "Article 32", "Article 44"], 0,
      "Article 14 promises equality before law to all persons.",
      tag="none/polity", expect=None),
    E("What does GDP measure in an economy?",
      ["Value of final goods", "Only farm output",
       "Only exports", "Only taxes"], 0,
      "GDP counts the value of final goods made inside a country.",
      tag="none/econ", expect=None),
    E("Which body keeps global peace and security?",
      ["UN Security Council", "WTO", "WHO", "ILO"], 0,
      "The UN Security Council keeps global peace and security.",
      tag="none/ir", expect=None),
    E("Cave paintings of ancient India are famous for what?",
      ["Natural colours", "Oil paints",
       "Glass work", "Steel art"], 0,
      "Ancient cave art used natural colours on rock walls.",
      tag="none/art", expect=None),
    E("What is H2O commonly known as?",
      ["Water", "Salt", "Oxygen", "Sugar"], 0,
      "H2O is the formula of plain drinking water.",
      tag="none/sci", expect=None),
    E("Who led the Dandi March for salt?",
      ["Mahatma Gandhi", "Nehru", "Patel", "Bose"], 0,
      "Gandhi led the march against the unjust salt tax.",
      tag="none/hist", expect=None),
    E("Which gas do plants absorb from air?",
      ["Carbon dioxide", "Oxygen", "Nitrogen", "Helium"], 0,
      "Plants absorb carbon dioxide to make their food.",
      tag="none/sci2", expect=None),
    E("What causes day and night on earth?",
      ["Earth rotation", "Moon rotation",
       "Sun rotation", "Star motion"], 0,
      "Rotation of the earth causes day and night in turn.",
      tag="none/geo", expect=None),
    E("Which house of Parliament is permanent?",
      ["Rajya Sabha", "Lok Sabha", "Both houses", "Neither"], 0,
      "Rajya Sabha never dissolves fully as a House.",
      tag="none/polity2", expect=None),
    E("What is the difference between direct and indirect tax?",
      ["Paid directly vs via goods", "Same burden",
       "Only customs", "Only gifts"], 0,
      "Taxes reach the state two ways.\n"
      "• Direct tax is paid on income earned.\n"
      "• Indirect tax is paid via goods bought.\n"
      "• Both taxes fund public services.",
      tag="econ/tax-compare"),
    E("What are the causes and effects of air pollution in detail?",
      ["Smoke harms lungs and sky", "Rain cleans all",
       "Wind stops fully", "Nothing harms"], 0,
      "Dirty air has clear sources.\n"
      "Causes:\n"
      "• Smoke from vehicles\n"
      "• Burning of waste\n"
      "Effects:\n"
      "• Lungs suffer disease\n"
      "• Skies turn hazy grey",
      tag="env/air-cause"),
    E("Explain the carbon cycle with its stages in detail.",
      ["Photosynthesis to respiration", "Only burning",
       "Only soil", "Only seas"], 0,
      "Carbon moves through stages.\n"
      "Stages:\n"
      "Photosynthesis, Consumption, Decomposition, Respiration",
      tag="env/carbon-cycle"),
    E("Describe the process of digestion in the human body.",
      ["Mouth to intestine", "Intestine to mouth",
       "Only stomach", "Only teeth"], 0,
      "Food travels through stages.\n"
      "1. Mouth chews the food well.\n"
      "2. Stomach churns the food mix.\n"
      "3. Intestine absorbs the nutrients.\n"
      "4. Waste leaves the body cleanly.",
      tag="sci/process3"),
    E("What is the difference between biotic and abiotic factors?",
      ["Living vs non-living", "Same factors",
       "Only soil", "Only water"], 0,
      "Nature has two factor types.\n"
      "• Biotic factors are living things.\n"
      "• Abiotic factors are non-living things.\n"
      "• Both shape every habitat jointly.",
      tag="env/compare"),
    E("Classify the following waste with examples in detail.",
      ["Biodegradable and more", "Only plastic",
       "Only paper", "Only metal"], 0,
      "Sort each waste item correctly.\n"
      "• Biodegradable: food scraps, paper\n"
      "• Non-biodegradable: plastic, glass\n"
      "• Hazardous: batteries, chemicals",
      tag="env/waste-class"),
    E("What are the three types of levers? Explain each briefly.",
      ["First, second, third order", "Only first",
       "Only wheels", "Only pulleys"], 0,
      "Levers differ by fulcrum place.\n"
      "• First order levers pivot in middle.\n"
      "• Second order levers lift at middle.\n"
      "• Third order levers push at middle.",
      tag="sci/levers-class"),
    E("The living cell comprises what basic parts?",
      ["Nucleus to mitochondria", "Only walls",
       "Only water", "Only air"], 0,
      "Read the parts below. The cell comprises the nucleus, cytoplasm, "
      "membrane and mitochondria for the life of all beings.",
      tag="sci/cell-concept"),
    E("Which metal is liquid at room temperature?",
      ["Mercury", "Iron", "Gold", "Silver"], 0,
      "Mercury stays liquid at room temperature unlike most metals.",
      tag="none/sci3", expect=None),
    E("Which organ purifies human blood?",
      ["Kidney", "Lung", "Heart", "Skin"], 0,
      "The kidney filters waste from human blood daily.",
      tag="none/sci4", expect=None),
    E("What is the currency of Japan?",
      ["Yen", "Won", "Dollar", "Euro"], 0,
      "Japan uses the Yen as its money.",
      tag="none/econ2", expect=None),
    E("Which line divides earth into two halves?",
      ["Equator", "Tropic", "Axis", "Orbit"], 0,
      "The equator divides the earth into two equal halves.",
      tag="none/geo2", expect=None),
    E("Who wrote the national anthem of India?",
      ["Tagore", "Iqbal", "Prasad", "Pant"], 0,
      "Tagore wrote the national anthem of India.",
      tag="none/art2", expect=None),
    E("Which text is the oldest Veda?",
      ["Rigveda", "Samaveda", "Yajurveda", "Atharvaveda"], 0,
      "Rigveda is the oldest of the four Vedas.",
      tag="none/hist2", expect=None),
]

SEEDS = SHOWCASE + EXTRA

# Verified-clean solution tokens for Hindi explanations (extraction of
# some Devanagari conjuncts is lossy, so these pin stable substrings).
_SOL = {"geo/map-hi": "सांभर झील", "none/hindi": "बाघ"}
for _e in SEEDS:
    if _e["tag"] in _SOL:
        _e["sol"] = _SOL[_e["tag"]]
