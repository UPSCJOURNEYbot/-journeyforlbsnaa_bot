"""Phase 3 milestone 3: volume question bank (offline, deterministic).

Hand-written realistic pools plus one map question per curated place.
`build_pool()` returns 300+ unique entries; the first ten are always
the SHOWCASE seeds so every size prefix covers every visual type.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.m3_seeds import E, SHOWCASE, EXTRA

ROOT = Path(__file__).resolve().parent.parent


def _timeline(stem, orders, correct, sentences):
    return E(stem, orders, correct,
             "Read the years in order. " + " ".join(sentences),
             tag="vol/timeline")


TIMELINES = [
    _timeline("Arrange the following early events in chronological order.",
              ["1885, 1905, 1907", "1905, 1907, 1885", "1907, 1885, 1905",
               "1885, 1907, 1905"], 0,
              ["A national congress was founded in 1885.",
               "A province was partitioned in 1905.",
               "A split divided the moderates in 1907."]),
    _timeline("Place these milestones in the correct sequence.",
              ["1915, 1917, 1919", "1917, 1919, 1915", "1919, 1915, 1917",
               "1915, 1919, 1917"], 0,
              ["A leader returned from abroad in 1915.",
               "A peasant struggle began in 1917.",
               "A massacre shocked the nation in 1919."]),
    _timeline("Which sequence of events is correct?",
              ["1920, 1922, 1928", "1922, 1928, 1920", "1928, 1920, 1922",
               "1920, 1928, 1922"], 0,
              ["A non-cooperation call came in 1920.",
               "A violent clash halted it in 1922.",
               "A commission was boycotted in 1928."]),
    _timeline("Order these developments chronologically.",
              ["1930, 1932, 1935", "1932, 1935, 1930", "1935, 1930, 1932",
               "1930, 1935, 1932"], 0,
              ["A salt march began in 1930.",
               "A pact settled a fast in 1932.",
               "A new government act came in 1935."]),
    _timeline("Arrange the following final events in chronological order.",
              ["1940, 1942, 1947", "1942, 1947, 1940", "1947, 1940, 1942",
               "1940, 1947, 1942"], 0,
              ["A demand for division rose in 1940.",
               "A quit call echoed in 1942.",
               "Freedom arrived at last in 1947."]),
    _timeline("Place these medieval milestones in the correct sequence.",
              ["1526, 1556, 1707", "1556, 1707, 1526", "1707, 1526, 1556",
               "1526, 1707, 1556"], 0,
              ["A northern battle founded rule in 1526.",
               "A young heir took the throne in 1556.",
               "A long reign ended at last in 1707."]),
    _timeline("Which sequence of reigns is correct?",
              ["1206, 1290, 1526", "1290, 1526, 1206", "1526, 1206, 1290",
               "1206, 1526, 1290"], 0,
              ["A slave line began to rule in 1206.",
               "A new house seized power in 1290.",
               "An empire fell in battle in 1526."]),
    _timeline("Order these southern events chronologically.",
              ["1336, 1509, 1565", "1509, 1565, 1336", "1565, 1336, 1509",
               "1336, 1565, 1509"], 0,
              ["A southern empire rose in 1336.",
               "A famed king was crowned in 1509.",
               "A great battle ended it in 1565."]),
    _timeline("Arrange the following western events in chronological order.",
              ["1674, 1761, 1818", "1761, 1818, 1674", "1818, 1674, 1761",
               "1674, 1818, 1761"], 0,
              ["A coronation built a kingdom in 1674.",
               "A northern battle broke pride in 1761.",
               "A final war ended the rule in 1818."]),
    _timeline("Place these republic milestones in the correct sequence.",
              ["1946, 1949, 1950", "1949, 1950, 1946", "1950, 1946, 1949",
               "1946, 1950, 1949"], 0,
              ["An assembly first gathered in 1946.",
               "A charter was adopted in 1949.",
               "A republic was born in 1950."]),
    _timeline("Which sequence of plans is correct?",
              ["1951, 1969, 1991", "1969, 1991, 1951", "1991, 1951, 1969",
               "1951, 1991, 1969"], 0,
              ["A first plan began in 1951.",
               "Major banks were taken over in 1969.",
               "Markets were opened up in 1991."]),
    _timeline("Order these world events chronologically.",
              ["1789, 1914, 1945", "1914, 1945, 1789", "1945, 1789, 1914",
               "1789, 1945, 1914"], 0,
              ["A revolution toppled a throne in 1789.",
               "A global war erupted in 1914.",
               "A second war ended in 1945."]),
    _timeline("Arrange the following discoveries in chronological order.",
              ["1905, 1928, 1953", "1928, 1953, 1905", "1953, 1905, 1928",
               "1905, 1953, 1928"], 0,
              ["A genius reframed time in 1905.",
               "A mould gave a drug in 1928.",
               "A helix revealed life in 1953."]),
    _timeline("Place these temple-era milestones in the correct sequence.",
              ["1000, 1200, 1500", "1200, 1500, 1000", "1500, 1000, 1200",
               "1000, 1500, 1200"], 0,
              ["A temple town grew around 1000.",
               "Sea trade peaked near 1200.",
               "A poet sang of gods in 1500."]),
    _timeline("Which sequence of struggles is correct?",
              ["1905, 1911, 1916", "1911, 1916, 1905", "1916, 1905, 1911",
               "1905, 1916, 1911"], 0,
              ["A partition sparked protest in 1905.",
               "A royal visit shifted a capital in 1911.",
               "A pact united two leagues in 1916."]),
    _timeline("Order these mass movements chronologically.",
              ["1919, 1920, 1922", "1920, 1922, 1919", "1922, 1919, 1920",
               "1919, 1922, 1920"], 0,
              ["A massacre inflamed minds in 1919.",
               "A boycott call spread in 1920.",
               "A clash called it off in 1922."]),
    _timeline("Arrange the following decisive events in chronological order.",
              ["1928, 1929, 1930", "1929, 1930, 1928", "1930, 1928, 1929",
               "1928, 1930, 1929"], 0,
              ["A report proposed reforms in 1928.",
               "A midnight flag promised freedom in 1929.",
               "A march to the sea began in 1930."]),
    _timeline("Place these provincial events in the correct sequence.",
              ["1935, 1937, 1939", "1937, 1939, 1935", "1939, 1935, 1937",
               "1935, 1939, 1937"], 0,
              ["A federal act was passed in 1935.",
               "Elected ministries took office in 1937.",
               "A war call forced resignations in 1939."]),
    _timeline("Which sequence of final years is correct?",
              ["1942, 1946, 1947", "1946, 1947, 1942", "1947, 1942, 1946",
               "1942, 1947, 1946"], 0,
              ["A quit call shook the raj in 1942.",
               "A naval revolt erupted in 1946.",
               "Two nations were born in 1947."]),
    _timeline("Order these reform events chronologically.",
              ["1829, 1856, 1891", "1856, 1891, 1829", "1891, 1829, 1856",
               "1829, 1891, 1856"], 0,
              ["A cruel rite was banned in 1829.",
               "Widows won remarriage rights in 1856.",
               "A consent age was fixed in 1891."]),
]


def _art(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/article")


ARTICLES = [
    _art("Which Article bans discrimination by the state?",
         ["Article 15", "Article 19", "Article 32", "Article 50"], 0,
         "Article 15 bans discrimination on listed grounds by the state."),
    _art("Which Article promises equal opportunity in public jobs?",
         ["Article 16", "Article 14", "Article 21", "Article 44"], 0,
         "Article 16 promises equal opportunity in public employment."),
    _art("Which Article protects the six freedoms of citizens?",
         ["Article 19", "Article 15", "Article 32", "Article 72"], 0,
         "Article 19 protects speech, assembly and four more freedoms."),
    _art("Which Article protects life and personal liberty?",
         ["Article 21", "Article 19", "Article 44", "Article 50"], 0,
         "Article 21 protects life and personal liberty of all persons."),
    _art("Which Article gives the right to constitutional remedies?",
         ["Article 32", "Article 21", "Article 19", "Article 15"], 0,
         "Article 32 lets citizens move the top court for rights."),
    _art("Which Article asks the state to try for a uniform civil code?",
         ["Article 44", "Article 32", "Article 14", "Article 21"], 0,
         "Article 44 is a directive asking for a uniform civil code."),
    _art("Which Article separates the judiciary from the executive?",
         ["Article 50", "Article 44", "Article 32", "Article 19"], 0,
         "Article 50 directs separation of courts from the executive."),
    _art("Which Article lists the fundamental duties of citizens?",
         ["Article 51A", "Article 50", "Article 44", "Article 32"], 0,
         "Article 51A lists duties like respecting the Constitution."),
    _art("Which Article lets the President grant pardons?",
         ["Article 72", "Article 74", "Article 76", "Article 78"], 0,
         "Article 72 lets the President pardon or commute sentences."),
    _art("Which Article provides a Council of Ministers for the President?",
         ["Article 74", "Article 72", "Article 76", "Article 110"], 0,
         "Article 74 provides ministers to aid and advise the President."),
    _art("Which Article creates the office of Attorney General?",
         ["Article 76", "Article 74", "Article 72", "Article 78"], 0,
         "Article 76 creates the top law officer post of the country."),
    _art("Which Article defines a Money Bill?",
         ["Article 110", "Article 112", "Article 123", "Article 108"], 0,
         "Article 110 defines Money Bills on taxes and funds."),
    _art("Which Article requires the annual financial statement?",
         ["Article 112", "Article 110", "Article 123", "Article 280"], 0,
         "Article 112 requires the yearly budget statement in the House."),
    _art("Which Article lets the President issue ordinances?",
         ["Article 123", "Article 112", "Article 110", "Article 72"], 0,
         "Article 123 allows ordinances when the House is not sitting."),
    _art("Who appoints the Governor of a state?",
         ["The President", "The Chief Minister", "The Speaker",
          "The Chief Justice"], 0,
         "Article 155 says the President appoints each state Governor."),
    _art("Which Article gives writ power to High Courts?",
         ["Article 226", "Article 32", "Article 136", "Article 227"], 0,
         "Article 226 gives wide writ power to the High Courts."),
    _art("Which Article creates the Finance Commission?",
         ["Article 280", "Article 263", "Article 312", "Article 324"], 0,
         "Article 280 creates the body sharing funds with states."),
    _art("Which Article creates the Election Commission?",
         ["Article 324", "Article 320", "Article 315", "Article 280"], 0,
         "Article 324 vests polls in an independent commission."),
    _art("Which Article declares Hindi the official language?",
         ["Article 343", "Article 344", "Article 348", "Article 351"], 0,
         "Article 343 declares Hindi in Devanagari script as official."),
    _art("Which Article covers a national emergency?",
         ["Article 352", "Article 356", "Article 360", "Article 368"], 0,
         "Article 352 covers emergency on war or armed rebellion grounds."),
    _art("Which Article covers failure of state machinery?",
         ["Article 356", "Article 352", "Article 360", "Article 365"], 0,
         "Article 356 covers President rule on machinery failure."),
    _art("Which Article gives Parliament the power to amend the charter?",
         ["Article 368", "Article 356", "Article 352", "Article 360"], 0,
         "Article 368 lays down the power and steps of amendment."),
    _art("Which schedule lists the official languages?",
         ["Eighth", "Seventh", "Ninth", "Tenth"], 0,
         "The eighth list names the recognised official languages."),
    _art("Which schedule covers land reform laws?",
         ["Ninth", "Eighth", "Seventh", "Fifth"], 0,
         "The ninth list shields land reform laws from challenge."),
    _art("Which Article abolishes untouchability in all forms?",
         ["Article 17", "Article 15", "Article 18", "Article 23"], 0,
         "Article 17 ends untouchability and bans its practice."),
    _art("Which Article bars the state from giving titles?",
         ["Article 18", "Article 17", "Article 19", "Article 20"], 0,
         "Article 18 bars titles except military and scholar honours."),
    _art("Which Article guards against unfair conviction?",
         ["Article 20", "Article 21", "Article 22", "Article 19"], 0,
         "Article 20 bars retrospective and double punishment."),
    _art("Which Article guards arrested persons in custody?",
         ["Article 22", "Article 20", "Article 21", "Article 32"], 0,
         "Article 22 gives counsel and court review to arrestees."),
    _art("Which Article protects minority culture and script?",
         ["Article 29", "Article 30", "Article 25", "Article 28"], 0,
         "Article 29 guards language, script and culture of groups."),
    _art("Which Article asks the state to run village panchayats?",
         ["Article 40", "Article 39", "Article 44", "Article 48"], 0,
         "Article 40 asks the state to build village councils."),
    _art("Which Article divides law topics into three lists?",
         ["Article 246", "Article 245", "Article 249", "Article 250"],
         0, "Article 246 splits topics into union, state and joint lists."),
    _art("Which Article covers inter-state water disputes?",
         ["Article 262", "Article 263", "Article 260", "Article 261"], 0,
         "Article 262 lets Parliament judge river water rows."),
    _art("Which Article makes property a legal right?",
         ["Article 300A", "Article 300", "Article 301", "Article 299"],
         0, "Article 300A keeps property as a legal right only."),
    _art("Which Article creates all-India services?",
         ["Article 312", "Article 310", "Article 315", "Article 320"], 0,
         "Article 312 allows creation of shared national services."),
]


def _sci(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/science")


SCIENCE = [
    _sci("The atom comprises what basic particles?",
         ["Protons to electrons", "Only protons", "Only air", "Only dust"],
         0,
         "Read the parts below. The atom comprises protons, neutrons, "
         "electrons and nucleus for the matter of all things."),
    _sci("What does blood comprise in the human body?",
         ["Cells to plasma", "Only water", "Only salt", "Only air"], 0,
         "Read the parts below. Blood comprises red cells, white cells, "
         "platelets and plasma for the health of all beings."),
    _sci("What are the three types of muscles? Explain each briefly.",
         ["Skeletal, smooth, cardiac", "Only skeletal", "Only bones",
          "Only skin"], 0,
         "Muscles fall into three families.\n"
         "• Skeletal muscles move the bones.\n"
         "• Smooth muscles line the organs.\n"
         "• Cardiac muscle drives the heart."),
    _sci("What are the three types of teeth? Explain each briefly.",
         ["Incisors, canines, molars", "Only molars", "Only gums",
          "Only tongue"], 0,
         "Teeth differ by their work.\n"
         "• Incisors cut the food first.\n"
         "• Canines tear the food next.\n"
         "• Molars grind the food fine."),
    _sci("Describe the process of respiration in humans.",
         ["Air to energy", "Energy to air", "Only nose", "Only chest"], 0,
         "Breathing runs in stages.\n"
         "1. Air enters through the nose.\n"
         "2. Lungs swap gases in sacs.\n"
         "3. Blood carries oxygen around.\n"
         "4. Cells release energy slowly."),
    _sci("Revise the key vitamins and their roles in detail.",
         ["All six vitamins", "Only one vitamin", "No vitamin",
          "Only pills"], 0,
         "Revise each point carefully.\n"
         "• Vitamin A guards eyesight and skin health in all seasons.\n"
         "• Vitamin B aids nerves and steady energy release each day.\n"
         "• Vitamin C heals wounds and fights common infections fast.\n"
         "• Vitamin D builds bones with the help of sunlight daily.\n"
         "• Vitamin E shields cells from damage by free radicals.\n"
         "• Vitamin K helps blood clot at cuts and wounds quickly."),
    _sci("What is the SI unit of force?",
         ["Newton", "Joule", "Watt", "Pascal"], 0,
         "Force is measured in newtons in the SI system."),
    _sci("What is the SI unit of electric current?",
         ["Ampere", "Volt", "Ohm", "Watt"], 0,
         "Current is measured in amperes in the SI system."),
    _sci("Which planet is called the Red Planet?",
         ["Mars", "Venus", "Jupiter", "Saturn"], 0,
         "Mars looks red due to iron-rich dust on it."),
    _sci("Which gas is most abundant in air?",
         ["Nitrogen", "Oxygen", "Carbon dioxide", "Hydrogen"], 0,
         "Nitrogen forms about four-fifths of the air."),
    _sci("What is the chemical formula of common salt?",
         ["NaCl", "KCl", "NaOH", "CaCO3"], 0,
         "Common salt is sodium chloride in pure form."),
    _sci("Which acid is present in lemons?",
         ["Citric acid", "Acetic acid", "Sulphuric acid", "Nitric acid"],
         0, "Lemons taste sour due to citric acid in them."),
    _sci("Which blood cells carry oxygen?",
         ["Red cells", "White cells", "Platelets", "Plasma cells"], 0,
         "Red cells carry oxygen through the whole body."),
    _sci("Which organelle is called the powerhouse of the cell?",
         ["Mitochondria", "Nucleus", "Ribosome", "Vacuole"], 0,
         "Mitochondria release energy inside living cells."),
    _sci("What is the normal human body temperature?",
         ["37 C", "30 C", "40 C", "45 C"], 0,
         "Healthy humans hold near thirty-seven degrees Celsius."),
    _sci("Which instrument measures atmospheric pressure?",
         ["Barometer", "Thermometer", "Ammeter", "Lactometer"], 0,
         "A barometer tracks the weight of the air above."),
    _sci("Which mirror is used in vehicle headlights?",
         ["Concave", "Convex", "Plane", "Cylindrical"], 0,
         "Concave mirrors throw a strong beam ahead."),
    _sci("What is the pH of neutral pure water?",
         ["Seven", "One", "Thirteen", "Zero"], 0,
         "Pure water is neutral at pH seven exactly."),
    _sci("Which vitamin deficiency causes night blindness?",
         ["Vitamin A", "Vitamin C", "Vitamin D", "Vitamin K"], 0,
         "Low vitamin A weakens vision in dim light."),
    _sci("Which metal is the best conductor of electricity?",
         ["Silver", "Iron", "Lead", "Zinc"], 0,
         "Silver conducts better than common metals."),
    _sci("What is the hardest natural substance?",
         ["Diamond", "Quartz", "Steel", "Granite"], 0,
         "Diamond tops the hardness scale of nature."),
    _sci("Which law explains falling apples and tides?",
         ["Gravitation", "Refraction", "Induction", "Diffusion"], 0,
         "Gravitation pulls masses toward each other always."),
    _sci("Which wave needs no medium to travel?",
         ["Light", "Sound", "Water ripple", "Seismic"], 0,
         "Light crosses empty space without any medium."),
    _sci("What is the formula of ozone gas?",
         ["O3", "O2", "CO2", "H2O2"], 0,
         "Ozone holds three oxygen atoms per molecule."),
    _sci("Which particle has no electric charge?",
         ["Neutron", "Proton", "Electron", "Positron"], 0,
         "The neutron sits neutral inside the nucleus."),
    _sci("What is an alloy in simple words?",
         ["Metal mixture", "Pure metal", "Raw ore", "Salt mix"], 0,
         "An alloy blends metals for extra strength."),
    _sci("Which gas fills most bulbs and tubes?",
         ["Nitrogen", "Oxygen", "Chlorine", "Fluorine"], 0,
         "Cheap nitrogen fills bulbs and keeps them safe."),
    _sci("What is distillation in simple words?",
         ["Boil and condense", "Only freeze", "Only filter",
          "Only shake"], 0,
         "Distillation boils a mix and condenses the vapour."),
]


def _env(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/env")


ENV = [
    _env("What are the causes and effects of global warming in detail?",
         ["Gases heat earth and seas", "Trees cool all",
          "Ice grows fast", "Nothing warms"], 0,
         "The planet heats for reasons.\n"
         "Causes:\n"
         "• Burning of fossil fuels\n"
         "• Cutting of green forests\n"
         "Effects:\n"
         "• Ice melts at poles\n"
         "• Seas rise on coasts"),
    _env("What are the causes and effects of soil erosion in detail?",
         ["Water and wind strip soil", "Worms build soil",
          "Rain feeds soil", "Roots harm soil"], 0,
         "Topsoil is lost by forces.\n"
         "Causes:\n"
         "• Heavy rain runoff\n"
         "• Overgrazing by herds\n"
         "Effects:\n"
         "• Farms lose fertility\n"
         "• Rivers silt up fast"),
    _env("What is the difference between weather and climate?",
         ["Daily vs average", "Same thing",
          "Only rain", "Only wind"], 0,
         "Both describe the air differently.\n"
         "• Weather changes from day to day.\n"
         "• Climate averages over long years.\n"
         "• Both guide farmers and planners."),
    _env("Which layer blocks harmful sun rays?",
         ["Ozone layer", "Dust layer", "Cloud layer", "Smoke layer"], 0,
         "The ozone layer blocks harmful ultraviolet rays."),
    _env("What does biodiversity mean in nature?",
         ["Variety of life", "Only tigers", "Only trees", "Only fish"],
         0, "Biodiversity means the rich variety of living things."),
    _env("Which fuel is the cleanest for cooking?",
         ["Biogas", "Coal", "Kerosene", "Wood"], 0,
         "Biogas burns clean from farm and home waste."),
    _env("What is compost made from?",
         ["Kitchen and farm waste", "Plastic", "Glass", "Metal"], 0,
         "Compost grows from rotting kitchen and farm waste."),
    _env("Which practice saves groundwater best?",
         ["Rain harvesting", "Deep boring", "Flooding fields",
          "Concrete drains"], 0,
         "Rain harvesting refills the wells below the ground."),
    _env("What is an ecosystem in simple words?",
         ["Living plus non-living unit", "Only forests",
          "Only rivers", "Only deserts"], 0,
         "An ecosystem links living things with air, water and soil."),
    _env("Which waste rots fastest in soil?",
         ["Food scraps", "Plastic", "Glass", "Metal"], 0,
         "Food scraps rot fast unlike plastic or glass."),
    _env("What is the main source of air in homes?",
         ["Ventilation", "Cans", "Balloons", "Pipes"], 0,
         "Good ventilation keeps indoor air fresh and safe."),
    _env("Which energy never runs out?",
         ["Solar", "Coal", "Oil", "Gas"], 0,
         "Solar energy flows daily without running out."),
    _env("What is afforestation in simple words?",
         ["Planting new forests", "Cutting forests", "Burning farms",
          "Drying lakes"], 0,
         "Afforestation means planting forests on bare lands."),
    _env("Which bag is kindest to nature?",
         ["Cloth bag", "Plastic bag", "Foil bag", "Foam bag"], 0,
         "Cloth bags reuse well unlike single-use plastic."),
    _env("What is a food chain in nature?",
         ["Eating links", "Iron links", "Trade links", "Road links"], 0,
         "A food chain links who eats whom in nature."),
    _env("Wetlands are precious mainly because they do what?",
         ["Clean water and shelter birds", "Grow plastic",
          "Make smoke", "Hide waste"], 0,
         "Wetlands clean water and shelter many birds."),
]


def _econ(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/econ")


ECON = [
    _econ("How does a bank give a farm loan? List the stages.",
          ["Apply, check, sanction, repay", "Only repay",
           "Only apply", "No check"], 0,
         "Loans follow a fixed procedure.\n"
         "1. The farmer applies with papers.\n"
         "2. The bank checks land and need.\n"
         "3. The loan is sanctioned duly.\n"
         "4. Instalments repay the loan."),
    _econ("What are the causes and effects of unemployment in detail?",
          ["Few jobs hurt youth", "Many jobs hurt all",
           "Farms hire all", "None suffer"], 0,
         "Joblessness has clear roots.\n"
         "Causes:\n"
         "• Slow growth of industry\n"
         "• Skills mismatch in youth\n"
         "Effects:\n"
         "• Poverty rises in homes\n"
         "• Unrest grows in streets"),
    _econ("What is the difference between a bank and a moneylender?",
          ["Regulated vs informal", "Same rules",
           "Only profits", "Only gifts"], 0,
          "Both lend but differ in rules.\n"
          "• A bank follows strict public rules.\n"
          "• A moneylender follows private terms.\n"
          "• Both charge for the money lent."),
    _econ("Who issues currency notes in India?",
          ["Reserve Bank", "State Bank", "Post Office", "Treasury"], 0,
         "The Reserve Bank issues notes in the country."),
    _econ("What is inflation in simple words?",
          ["Prices rising", "Prices falling", "Coins shining",
           "Notes tearing"], 0,
         "Inflation means prices rising over time."),
    _econ("What is a budget in simple words?",
          ["Income and spending plan", "Only taxes",
           "Only loans", "Only gifts"], 0,
         "A budget plans income against spending ahead."),
    _econ("What does GST unify across states?",
          ["Indirect taxes", "Direct taxes", "Court fees", "Bus fares"],
         0, "GST unifies many indirect taxes into one."),
    _econ("What is a recession in simple words?",
          ["Output shrinking", "Output booming", "Rains failing",
           "Crowds rising"], 0,
         "A recession means output shrinking for months."),
    _econ("What is poverty line based on?",
          ["Minimum needs", "Tall buildings", "Car counts", "TV sets"],
         0, "The line marks minimum needs for a basic life."),
    _econ("What is export in simple words?",
          ["Selling abroad", "Buying abroad", "Storing grain",
           "Melting coins"], 0,
         "Export means selling goods to foreign buyers."),
    _econ("What is import in simple words?",
          ["Buying from abroad", "Selling abroad", "Growing grain",
           "Minting coins"], 0,
         "Import means buying goods from foreign sellers."),
    _econ("Which sector employs most Indians?",
          ["Agriculture", "Films", "Sports", "Mining"], 0,
         "Farms still employ the most workers in India."),
    _econ("What is a loan waiver in simple words?",
          ["Dues forgiven", "Dues doubled", "Rates raised",
           "Banks closed"], 0,
         "A waiver forgives the dues of borrowers."),
    _econ("What is minimum support price for?",
          ["Farm produce", "Movie tickets", "Bus fares", "Phone bills"],
         0, "Support price assures farmers a floor rate."),
    _econ("What is demonetisation in simple words?",
          ["Notes withdrawn", "Notes printed", "Coins polished",
           "Banks painted"], 0,
         "Demonetisation withdraws notes from circulation."),
    _econ("What is repo rate in simple words?",
          ["Bank borrowing cost", "Bus fare", "Grain price", "Film tax"],
         0, "Repo rate is what banks pay to borrow short."),
    _econ("What is NITI Aayog in simple words?",
          ["Policy think tank", "Film board", "Sports club",
           "Trade fair"], 0,
         "NITI Aayog is the policy think tank of India."),
    _econ("What is CRR in simple words?",
          ["Reserve with central bank", "Bus fare", "Grain price",
           "Film tax"], 0,
         "CRR parks a share of deposits with the central bank."),
    _econ("What is SLR in simple words?",
          ["Reserve in safe assets", "Bus fare", "Grain price",
           "Film tax"], 0,
         "SLR locks a share of funds in safe assets."),
    _econ("What is fiscal deficit in simple words?",
          ["Spending over income", "Income over spending",
           "Equal books", "No books"], 0,
         "Fiscal deficit is spending running over income."),
]


def _ir(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/ir")


IR = [
    _ir("What is the difference between bilateral and multilateral talks?",
        ["Two vs many sides", "Same sides", "Only trade",
         "Only war"], 0,
        "Talks differ by sides present.\n"
        "• Bilateral talks involve two sides only.\n"
        "• Multilateral talks involve many sides.\n"
        "• Both seek deals through dialogue."),
    _ir("Where is the UN head office located?",
        ["New York", "Paris", "Rome", "Delhi"], 0,
        "The UN head office is located in New York city."),
    _ir("What does WTO regulate among nations?",
        ["Trade rules", "Sea tides", "Moon maps", "Star names"], 0,
        "The WTO sets fair trade rules for members."),
    _ir("What is SAARC in simple words?",
        ["South Asian group", "Space club", "Ocean pact",
         "Desert forum"], 0,
        "SAARC groups South Asian nations for talks."),
    _ir("What is the G20 in simple words?",
        ["Top economies forum", "Cricket league", "Film gala",
         "Food fest"], 0,
        "The G20 gathers top economies to coordinate policy."),
    _ir("What is an embassy in simple words?",
        ["Nation office abroad", "Ship cabin", "Train coach",
         "Hotel room"], 0,
        "An embassy is a nation office on foreign soil."),
    _ir("What is a treaty in simple words?",
        ["Written pact", "Spoken rumour", "Old coin", "War cry"], 0,
        "A treaty is a written pact between nations."),
    _ir("What is diplomacy in simple words?",
        ["Talk over war", "War over talk", "Trade over all",
         "Silence always"], 0,
        "Diplomacy solves rows through talk first."),
    _ir("What is a summit in simple words?",
        ["Leaders meet", "Mountains rise", "Rivers join",
         "Crowds march"], 0,
        "A summit is a meet of top leaders."),
    _ir("What is foreign aid in simple words?",
        ["Help across borders", "Tax at home", "Loan to self",
         "Gift to self"], 0,
        "Foreign aid is help flowing across borders."),
]


def _art(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/art")


ART = [
    _art("What are the three classical dance families? Name each briefly.",
         ["Temple, court, folk-rooted", "Only films",
          "Only streets", "Only circuses"], 0,
         "Dances fall into three families.\n"
         "• Temple dances serve devotion rites.\n"
         "• Court dances serve royal halls.\n"
         "• Folk-rooted dances serve harvest joy."),
    _art("What are the three folk theatre forms? Name each briefly.",
         ["Song, dance, mime-led", "Only films",
          "Only news", "Only sports"], 0,
         "Folk stages differ by craft.\n"
         "• Song-led plays sing the story.\n"
         "• Dance-led plays move the story.\n"
         "• Mime-led plays act without words."),
    _art("Which festival marks harvest joy in fields?",
         ["Harvest festival", "War day", "Fast day", "Mourning day"],
         0, "Harvest festivals thank the fields and rains."),
    _art("What is miniature painting famous for?",
         ["Fine detail", "Big walls", "Steel frames", "Glass shine"],
         0, "Miniatures pack fine detail into small frames."),
    _art("What is classical music built on?",
         ["Raga and tala", "Noise and haste", "Echo and fog",
          "Drums only"], 0,
         "Raga gives tune and tala gives rhythm."),
    _art("What is folk art rooted in?",
         ["Village life", "Palace vaults", "Bank lockers",
          "Court files"], 0,
         "Folk art mirrors daily village life and lore."),
    _art("What are epics in simple words?",
         ["Long hero tales", "Short jokes", "Tax lists",
          "Train charts"], 0,
         "Epics sing long tales of heroes and gods."),
    _art("What is a stupa in simple words?",
         ["Relic mound", "War tower", "Grain silo", "Clock tower"],
         0, "A stupa is a mound holding sacred relics."),
    _art("What is a temple tank used for?",
         ["Ritual bathing", "Boat races", "Fish trade",
          "Salt making"], 0,
         "Devotees bathe in temple tanks before prayer."),
    _art("What is rangoli made of?",
         ["Coloured powder", "Melted iron", "Cut glass",
          "Wet clay"], 0,
         "Rangoli blooms in coloured powder at doors."),
    _art("What is puppetry in simple words?",
         ["Doll theatre", "Stone carving", "Metal casting",
          "Glass blowing"], 0,
         "Puppetry stages tales with dolls on strings."),
    _art("What is a fair called in villages?",
         ["Mela", "Bazaar tax", "Court fine", "Toll gate"], 0,
         "A mela gathers trade, play and prayer."),
]


def _hist(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/hist")


HIST = [
    _hist("What is the difference between Moderates and Extremists?",
          ["Petition vs agitation", "Same methods",
           "Only prayers", "Only silence"], 0,
          "Both fought for freedom differently.\n"
          "• Moderates trusted petitions and talks.\n"
          "• Extremists trusted boycott and stir.\n"
          "• Both loved the motherland dearly."),
    _hist("What is the difference between a kingdom and an empire?",
          ["One vs many lands", "Same size",
           "Only forts", "Only coins"], 0,
          "Both are realms of different scale.\n"
          "• A kingdom rules one core land.\n"
          "• An empire rules many lands afar.\n"
          "• Both need armies and taxes."),
    _hist("Who founded the Mauryan line of kings?",
          ["Chandragupta", "Ashoka", "Bindusara", "Kanishka"], 0,
          "Chandragupta founded the first great Indian empire."),
    _hist("Which king spread dhamma on rocks and pillars?",
          ["Ashoka", "Harsha", "Samudra", "Pulakeshin"], 0,
          "Ashoka preached dhamma after a bloody war."),
    _hist("Who wrote the famous grammar of Sanskrit?",
          ["Panini", "Kalidasa", "Aryabhata", "Charaka"], 0,
          "Panini set the rules of Sanskrit grammar."),
    _hist("Who is called the father of surgery in lore?",
          ["Sushruta", "Charaka", "Vagbhata", "Jivaka"], 0,
          "Sushruta described surgery in an old text."),
    _hist("Which age is called the golden age of art?",
          ["Gupta age", "Late age", "Dark age", "Iron age"], 0,
          "Gupta times shone in art and learning."),
    _hist("Who led the revolt of the sepoys first?",
          ["Mangal Pandey", "Tantia Tope", "Nana Saheb", "Rani Lakshmi"],
          0, "Mangal Pandey struck the first blow of revolt."),
    _hist("Which queen fought bravely in the revolt?",
          ["Rani of Jhansi", "Raziya", "Noor Jahan", "Durgavati"], 0,
          "The brave queen died fighting the foreign troops."),
    _hist("Who gave the call of full freedom at midnight?",
          ["Nehru", "Patel", "Bose", "Azad"], 0,
          "Nehru spoke of a tryst with destiny."),
    _hist("Who built a strong navy for the western kingdom?",
          ["Shivaji", "Sambhaji", "Rajaram", "Shahu"], 0,
          "Shivaji built forts and ships for sea defence."),
    _hist("Who wrote the Ramcharitmanas for the common folk?",
          ["Tulsidas", "Surdas", "Kabir", "Meera"], 0,
          "Tulsidas sang the tale in the tongue of the folk."),
    _hist("Who preached one god in short couplets?",
          ["Kabir", "Nanak", "Chaitanya", "Basava"], 0,
          "Kabir mocked priests and praised one formless god."),
]


def _geo(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/geo")


GEO = [
    _geo("What are the three layers of the earth? Name each briefly.",
         ["Crust, mantle, core", "Only crust",
          "Only sky", "Only seas"], 0,
         "Earth hides three layers inside.\n"
         "• Crust forms the thin outer skin.\n"
         "• Mantle flows hot and slow within.\n"
         "• Core burns dense at centre."),
    _geo("What are the three types of rainfall? Name each briefly.",
         ["Relief, convectional, cyclonic", "Only hail",
          "Only dew", "Only fog"], 0,
         "Rains fall in three ways.\n"
         "• Relief rain strikes hillsides.\n"
         "• Convectional rain rises with heat.\n"
         "• Cyclonic rain swirls with storms."),
    _geo("What is the difference between a river and a canal?",
          ["Natural vs man-made", "Same flow",
           "Only dams", "Only wells"], 0,
          "Both carry water but differ in birth.\n"
          "• A river is born of nature.\n"
          "• A canal is built by humans.\n"
          "• Both water farms and towns."),
    _geo("What is IST based on in simple words?",
         ["A central meridian", "Moon phase", "Star clock",
          "Sea tide"], 0,
         "IST follows a central meridian of the land."),
    _geo("Which tropic crosses the middle of India?",
         ["Tropic of Cancer", "Tropic of Capricorn",
          "Arctic circle", "Equator"], 0,
         "The Tropic of Cancer crosses eight states."),
    _geo("What is a delta in simple words?",
         ["River mouth fan", "Hill top", "Sea floor",
          "Desert dune"], 0,
         "A delta fans where a river meets the sea."),
    _geo("What is a glacier in simple words?",
         ["Moving ice mass", "Sand storm", "Hot spring",
          "Lava flow"], 0,
         "A glacier is a slow river of ice."),
    _geo("What is a peninsula in simple words?",
         ["Land ringed by sea", "Deep well", "Tall tower",
          "Long road"], 0,
         "A peninsula juts with sea on three sides."),
    _geo("What is an island in simple words?",
         ["Land ringed by water", "Hill fort", "River dam",
          "Sea port"], 0,
         "An island is land ringed fully by water."),
    _geo("What is a strait in simple words?",
         ["Narrow sea lane", "Wide ocean", "Calm lake",
          "Dry desert"], 0,
         "A strait is a narrow lane joining seas."),
    _geo("What is a bay in simple words?",
         ["Sea inlet", "Hill pass", "Sand bar",
          "Rock arch"], 0,
         "A bay is a sea inlet curving inland."),
    _geo("What is a gulf in simple words?",
         ["Large sea inlet", "Small pond", "Dry pit",
          "Snow peak"], 0,
         "A gulf is a large inlet of the sea."),
    _geo("What is loam soil made of in simple words?",
         ["Sand, silt and clay", "Only sand", "Only rocks",
          "Only water"], 0,
         "Loam blends sand, silt and clay for crops."),
    _geo("What is an oasis in simple words?",
         ["Desert water spot", "Snow peak", "Sea wave",
          "Hill fort"], 0,
         "An oasis waters travellers in dry deserts."),
]


def _pol(stem, opts, correct, expl):
    return E(stem, opts, correct, expl, tag="vol/polity")


POLITY = [
    _pol("How does a bill become a law? List the stages.",
         ["Draft, debate, pass, assent", "Only assent",
          "Only draft", "No debate"], 0,
         "Laws follow a fixed procedure.\n"
         "1. A draft bill is prepared well.\n"
         "2. Members debate it in Houses.\n"
         "3. Both Houses pass the bill.\n"
         "4. Assent turns it into law."),
    _pol("How are elections held? List the stages.",
         ["Rolls, campaign, poll, count", "Only count",
          "Only polls", "No rolls"], 0,
         "Elections follow a fixed procedure.\n"
         "1. Voter rolls are revised first.\n"
         "2. Parties campaign for votes.\n"
         "3. Citizens cast votes in booths.\n"
         "4. Votes are counted with care."),
    _pol("Parliament comprises which houses and head?",
         ["Two Houses and President", "Only one House",
          "Only courts", "Only states"], 0,
         "Read the parts below. Parliament comprises the Lok Sabha, "
         "Rajya Sabha and President for the laws of the land."),
    _pol("Who is the head of a state government?",
         ["Chief Minister", "Governor", "Speaker", "Mayor"], 0,
         "The Chief Minister heads the elected state government."),
    _pol("Who heads a village panchayat?",
         ["Sarpanch", "Collector", "Judge", "Patwari"], 0,
         "The sarpanch heads the village council."),
    _pol("What is the term of the Lok Sabha?",
         ["Five years", "Six years", "Four years", "Three years"], 0,
         "The Lok Sabha sits for five years at most."),
    _pol("What is a no-confidence motion about?",
         ["Testing majority", "Praising all", "Raising funds",
          "Closing courts"], 0,
         "The motion tests if the government holds majority."),
    _pol("What is the quorum of a House in simple words?",
         ["Tenth of members", "Half of members", "All members",
          "Two members"], 0,
         "A tenth of members must be present to sit."),
]


def _hin(stem, opts, correct, expl, track, sol):
    entry = E(stem, opts, correct, expl, tag="vol/hindi", track=track)
    entry["sol"] = sol
    return entry


HINDI = [
    _hin("भारत की राजधानी कहाँ स्थित है?",
         ["Delhi", "Mumbai", "Jaipur", "Lucknow"], 0,
         "दिल्ली भारत की राजधानी है। यह उत्तर में स्थित है।",
         "राजधानी", "राजधानी"),
    _hin("गंगा नदी कहाँ से निकलती है?",
         ["Glacier", "Lake", "Sea", "Well"], 0,
         "गंगा हिमनद से निकलती है। यह मैदानों को सींचती है।",
         "नदी", "हिमनद"),
    _hin("घटनाओं को सही क्रम में लगाएँ।",
         ["1857, 1919, 1942", "1919, 1942, 1857",
          "1942, 1857, 1919", "1857, 1942, 1919"], 0,
         "वर्षों को क्रम से पढ़ें। 1857 में विद्रोह हुआ। "
         "1919 में दुखद घटना हुई। 1942 में आंदोलन छिड़ा।",
         "क्रम", "विद्रोह"),
    _hin("वनों के नाश के कारण और प्रभाव विस्तार से लिखें।",
         ["Cutting harms soil", "Planting harms all",
          "Rains stop fully", "Nothing changes"], 0,
         "वन धरती की रक्षा करते हैं।\n"
         "Causes:\n"
         "• पेड़ों की कटाई\n"
         "• खनन कार्य\n"
         "Effects:\n"
         "• मिट्टी का कटाव\n"
         "• जलवायु में बदलाव",
         "वनों", "खनन"),
    _hin("पोषण के मुख्य बिंदु विस्तार से दोहराएँ।",
         ["All six points", "Only one point", "No point",
          "Only pills"], 0,
         "प्रत्येक बिंदु ध्यान से दोहराएँ।\n"
         "• संतुलित भोजन शरीर को शक्ति देता है हर दिन।\n"
         "• हरी सब्जियाँ विटामिन देती हैं भरपूर मात्रा में।\n"
         "• दूध हड्डियों को मजबूत बनाता है बचपन से।\n"
         "• पानी शरीर की सफाई करता है हर समय।\n"
         "• फल रोगों से रक्षा करते हैं नियमित सेवन पर।\n"
         "• अनाज ऊर्जा देता है काम के हर घंटे में।",
         "पोषण", "अनाज"),
    _hin("भारत का राष्ट्रीय खेल कौन सा माना जाता है?",
         ["Hockey", "Cricket", "Football", "Chess"], 0,
         "हॉकी को राष्ट्रीय खेल माना जाता है। यह गौरव का विषय है।",
         "राष्ट्रीय खेल", "हॉकी"),
]


def _place_entries():
    data = json.loads(
        (ROOT / "pdf_service" / "viz" / "geo_places.json").read_text())
    places = data["places"]
    kinds = {"city": "It is an important urban centre.",
             "river": "It is an important river system.",
             "lake": "It is an important lake.",
             "mountain": "It is an important peak.",
             "pass": "It is an important mountain pass.",
             "plateau": "It is an important plateau.",
             "desert": "It is an important desert.",
             "island": "It is an important island.",
             "state": "It is an important state.",
             "country": "It is an important country.",
             "dam": "It is an important dam.",
             "waterfall": "It is an important waterfall.",
             "sea": "It is an important sea.",
             "ocean": "It is an important ocean.",
             "gulf": "It is an important gulf.",
             "strait": "It is an important strait.",
             "bay": "It is an important bay.",
             "peninsula": "It is an important peninsula.",
             "glacier": "It is an important glacier.",
             "volcano": "It is an important volcano.",
             "forest": "It is an important forest.",
             "park": "It is an important park."}
    entries = []
    n = len(places)
    for i, place in enumerate(places):
        name = place["name_en"]
        if name == "Malé":
            name = "Male City"  # ASCII alias that still matches map data
        others = []
        for k in (1, 2, 3):
            other = places[(i + k) % n]["name_en"]
            others.append("Male City" if other == "Malé" else other)
        correct = i % 4
        opts = list(others)
        opts.insert(correct, name)
        if place.get("region") == "india":
            where = "located in India"
        else:
            where = "located outside India"
        kind = kinds.get(place.get("kind"), "It is an important place.")
        expl = "%s is %s. %s" % (name, where, kind)
        entries.append(E("Where is %s located?" % name, opts, correct,
                         expl, tag="vol/place"))
    return entries


HAND = (HINDI + TIMELINES + ARTICLES + SCIENCE + ENV + ECON + IR
        + ART + HIST + GEO + POLITY)


def default_track(stem):
    return " ".join(stem.split())[:40]


def build_pool():
    """Full deterministic pool: SHOWCASE first, then hand+places woven."""
    pool = list(SHOWCASE)
    rest = list(EXTRA) + list(HAND)
    places = _place_entries()
    woven = []
    for i, entry in enumerate(rest):
        woven.append(entry)
        if i % 3 == 2 and places:
            woven.append(places.pop(0))
    woven.extend(places)
    pool.extend(woven)
    assert len(pool) >= 310, len(pool)
    seen = set()
    for entry in pool:
        track = entry["track"] or default_track(entry["stem"])
        assert track not in seen, "duplicate track: %r" % track
        seen.add(track)
    return pool
