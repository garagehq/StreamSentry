"""
Ad Blocker Overlay for Minus.

Displays a blocking overlay when ads are detected on screen.
Uses ustreamer's native blocking mode for smooth 60fps overlays and animations.

Architecture:
- Simple GStreamer pipeline with queue element for smooth video display
- All overlay compositing done in ustreamer's MPP encoder (60fps preview!)
- Control via HTTP API to ustreamer's /blocking endpoints

Features:
- 60fps live preview window (vs ~4fps with GStreamer gdkpixbufoverlay)
- Smooth animations via rapid API updates
- Spanish vocabulary practice during ad blocks
- Pixelated background from pre-ad content
"""

import os
import threading
import time
import random
import logging
import urllib.request
import urllib.parse
import json
from collections import deque

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

# Set up logging
logger = logging.getLogger(__name__)

# Spanish vocabulary - beginner and intermediate level
# Format: (spanish, pronunciation, english, example_sentence)
# Pronunciation uses simplified phonetics: capitals indicate stressed syllables
SPANISH_VOCABULARY = [
    # ═══════════════════════════════════════════════════════════════════
    # COMMON VERBS (Beginner)
    # ═══════════════════════════════════════════════════════════════════
    ("hablar", "ah-BLAR", "to speak", "Hablo espanol todos los dias."),
    ("comer", "koh-MEHR", "to eat", "Vamos a comer tacos."),
    ("vivir", "bee-BEER", "to live", "Vivo en una ciudad grande."),
    ("ser", "sehr", "to be (permanent)", "Soy de Mexico."),
    ("estar", "ehs-TAR", "to be (temporary)", "Estoy cansado hoy."),
    ("tener", "teh-NEHR", "to have", "Tengo dos hermanos."),
    ("hacer", "ah-SEHR", "to do/make", "Hago la tarea cada noche."),
    ("ir", "eer", "to go", "Voy al supermercado."),
    ("poder", "poh-DEHR", "to be able to/can", "Puedo ayudarte manana."),
    ("querer", "keh-REHR", "to want/love", "Quiero aprender mas."),
    ("saber", "sah-BEHR", "to know (facts)", "Se la respuesta."),
    ("conocer", "koh-noh-SEHR", "to know (people/places)", "Conozco a tu hermana."),
    ("decir", "deh-SEER", "to say/tell", "Dime la verdad."),
    ("venir", "beh-NEER", "to come", "Ven a mi casa."),
    ("salir", "sah-LEER", "to leave/go out", "Salgo a las ocho."),
    ("llegar", "yeh-GAR", "to arrive", "Llego tarde al trabajo."),
    ("poner", "poh-NEHR", "to put/place", "Pon los libros en la mesa."),
    ("dar", "dar", "to give", "Te doy un regalo."),
    ("ver", "behr", "to see", "Veo una pelicula."),
    ("pensar", "pehn-SAR", "to think", "Pienso en ti."),
    ("creer", "kreh-EHR", "to believe", "Creo que tienes razon."),
    ("encontrar", "ehn-kohn-TRAR", "to find", "Encontre mis llaves."),
    ("seguir", "seh-GEER", "to follow/continue", "Sigue todo recto."),
    ("llamar", "yah-MAR", "to call", "Te llamo manana."),
    ("sentir", "sehn-TEER", "to feel", "Siento mucho frio."),
    ("dejar", "deh-HAR", "to leave/let", "Deja la puerta abierta."),
    ("parecer", "pah-reh-SEHR", "to seem", "Parece interesante."),
    ("quedar", "keh-DAR", "to stay/remain", "Me quedo en casa."),
    ("pasar", "pah-SAR", "to pass/happen", "Que paso ayer?"),
    ("esperar", "ehs-peh-RAR", "to wait/hope", "Espero tu respuesta."),
    ("buscar", "boos-KAR", "to look for", "Busco un restaurante."),
    ("entrar", "ehn-TRAR", "to enter", "Entra por la puerta principal."),
    ("trabajar", "trah-bah-HAR", "to work", "Trabajo desde casa."),
    ("necesitar", "neh-seh-see-TAR", "to need", "Necesito tu ayuda."),
    ("llevar", "yeh-BAR", "to carry/wear", "Llevo una camisa azul."),
    ("empezar", "ehm-peh-SAR", "to begin", "Empiezo a las nueve."),
    ("terminar", "tehr-mee-NAR", "to finish", "Termine el proyecto."),
    ("abrir", "ah-BREER", "to open", "Abre la ventana."),
    ("cerrar", "seh-RAR", "to close", "Cierra la puerta."),
    ("escribir", "ehs-kree-BEER", "to write", "Escribo un correo."),
    ("leer", "leh-EHR", "to read", "Leo un libro cada mes."),
    ("dormir", "dohr-MEER", "to sleep", "Duermo ocho horas."),
    ("correr", "koh-REHR", "to run", "Corro en el parque."),
    ("caminar", "kah-mee-NAR", "to walk", "Camino al trabajo."),
    ("comprar", "kohm-PRAR", "to buy", "Compro frutas frescas."),
    ("vender", "behn-DEHR", "to sell", "Vendo mi carro viejo."),
    ("pagar", "pah-GAR", "to pay", "Pago con tarjeta."),
    ("preguntar", "preh-goon-TAR", "to ask", "Pregunto por direcciones."),
    ("responder", "rehs-pohn-DEHR", "to answer", "Respondo rapidamente."),
    ("ayudar", "ah-yoo-DAR", "to help", "Te ayudo con eso."),

    # ═══════════════════════════════════════════════════════════════════
    # COMMON VERBS (Intermediate)
    # ═══════════════════════════════════════════════════════════════════
    ("aprovechar", "ah-proh-beh-CHAR", "to take advantage of", "Hay que aprovechar el tiempo."),
    ("lograr", "loh-GRAR", "to achieve/manage", "Logre terminar el proyecto."),
    ("desarrollar", "deh-sah-roh-YAR", "to develop", "Vamos a desarrollar una app."),
    ("destacar", "dehs-tah-KAR", "to stand out", "Su trabajo destaca por su calidad."),
    ("enfrentar", "ehn-frehn-TAR", "to face/confront", "Debemos enfrentar los problemas."),
    ("realizar", "reh-ah-lee-SAR", "to carry out/accomplish", "Voy a realizar mi sueno."),
    ("averiguar", "ah-beh-ree-GWAR", "to find out", "Necesito averiguar la verdad."),
    ("pertenecer", "pehr-teh-neh-SEHR", "to belong", "Este libro pertenece a Maria."),
    ("alcanzar", "ahl-kahn-SAR", "to reach/achieve", "Quiero alcanzar mis metas."),
    ("surgir", "soor-HEER", "to arise/emerge", "Surgio un problema inesperado."),
    ("tardar", "tar-DAR", "to take time/be late", "Cuanto tardas en llegar?"),
    ("soler", "soh-LEHR", "to usually do", "Suelo desayunar temprano."),
    ("proponer", "proh-poh-NEHR", "to propose", "Propongo una solucion."),
    ("suponer", "soo-poh-NEHR", "to suppose", "Supongo que tienes razon."),
    ("mantener", "mahn-teh-NEHR", "to maintain", "Mantiene una actitud positiva."),
    ("obtener", "ohb-teh-NEHR", "to obtain", "Obtuve buenos resultados."),
    ("reconocer", "reh-koh-noh-SEHR", "to recognize", "Reconozco mi error."),
    ("producir", "proh-doo-SEER", "to produce", "Producimos mucho cafe."),
    ("reducir", "reh-doo-SEER", "to reduce", "Debemos reducir los gastos."),
    ("conducir", "kohn-doo-SEER", "to drive", "Conduzco con cuidado."),
    ("traducir", "trah-doo-SEER", "to translate", "Traduzco documentos."),
    ("elegir", "eh-leh-HEER", "to choose", "Elige tu favorito."),
    ("exigir", "ehk-see-HEER", "to demand", "Exijo una explicacion."),
    ("dirigir", "dee-ree-HEER", "to direct/lead", "Dirijo el proyecto."),
    ("influir", "een-floo-EER", "to influence", "Influye en mi decision."),
    ("contribuir", "kohn-tree-boo-EER", "to contribute", "Contribuyo al equipo."),
    ("construir", "kohns-troo-EER", "to build", "Construyen un edificio nuevo."),
    ("destruir", "dehs-troo-EER", "to destroy", "No destruyas el medio ambiente."),
    ("incluir", "een-kloo-EER", "to include", "Incluye todos los detalles."),
    ("concluir", "kohn-kloo-EER", "to conclude", "Concluyo mi presentacion."),
    ("huir", "oo-EER", "to flee", "Huyen del peligro."),
    ("advertir", "ahd-behr-TEER", "to warn", "Te advierto del riesgo."),
    ("convertir", "kohn-behr-TEER", "to convert", "Convierto dolares a euros."),
    ("invertir", "een-behr-TEER", "to invest", "Invierto en mi educacion."),
    ("divertir", "dee-behr-TEER", "to amuse", "Me divierto con amigos."),
    ("preferir", "preh-feh-REER", "to prefer", "Prefiero el cafe."),
    ("sugerir", "soo-heh-REER", "to suggest", "Sugiero una alternativa."),
    ("mentir", "mehn-TEER", "to lie", "Nunca miento."),
    ("medir", "meh-DEER", "to measure", "Mido la distancia."),
    ("pedir", "peh-DEER", "to ask for/order", "Pido la cuenta."),
    ("repetir", "reh-peh-TEER", "to repeat", "Repite la pregunta."),
    ("competir", "kohm-peh-TEER", "to compete", "Competimos en el torneo."),
    ("servir", "sehr-BEER", "to serve", "Sirvo la cena."),
    ("vestir", "behs-TEER", "to dress", "Me visto rapidamente."),
    ("conseguir", "kohn-seh-GEER", "to get/obtain", "Consegui el trabajo."),
    ("perseguir", "pehr-seh-GEER", "to pursue/chase", "Persigo mis suenos."),
    ("despedir", "dehs-peh-DEER", "to fire/say goodbye", "Me despido de ti."),
    ("impedir", "eem-peh-DEER", "to prevent", "Impido el acceso."),
    ("corregir", "koh-reh-HEER", "to correct", "Corrijo los errores."),
    ("recoger", "reh-koh-HEHR", "to pick up/collect", "Recojo a los ninos."),
    ("proteger", "proh-teh-HEHR", "to protect", "Protejo a mi familia."),
    ("escoger", "ehs-koh-HEHR", "to choose", "Escoge el mejor."),
    ("coger", "koh-HEHR", "to take/catch", "Cojo el autobus."),
    ("valer", "bah-LEHR", "to be worth", "Vale la pena intentar."),
    ("caber", "kah-BEHR", "to fit", "No cabe en la maleta."),
    ("caer", "kah-EHR", "to fall", "Me cai en la calle."),
    ("traer", "trah-EHR", "to bring", "Traigo buenas noticias."),
    ("ofrecer", "oh-freh-SEHR", "to offer", "Ofrezco mi ayuda."),
    ("aparecer", "ah-pah-reh-SEHR", "to appear", "Aparece en la pantalla."),
    ("desaparecer", "deh-sah-pah-reh-SEHR", "to disappear", "Desaparece sin explicacion."),
    ("crecer", "kreh-SEHR", "to grow", "Las plantas crecen rapido."),
    ("agradecer", "ah-grah-deh-SEHR", "to thank", "Agradezco tu ayuda."),
    ("establecer", "ehs-tah-bleh-SEHR", "to establish", "Establezco nuevas metas."),
    ("pertenecer", "pehr-teh-neh-SEHR", "to belong", "Pertenezco a este grupo."),
    ("merecer", "meh-reh-SEHR", "to deserve", "Mereces lo mejor."),
    ("nacer", "nah-SEHR", "to be born", "Naci en primavera."),
    ("obedecer", "oh-beh-deh-SEHR", "to obey", "Obedece las reglas."),

    # ═══════════════════════════════════════════════════════════════════
    # REFLEXIVE VERBS
    # ═══════════════════════════════════════════════════════════════════
    ("levantarse", "leh-bahn-TAR-seh", "to get up", "Me levanto a las siete."),
    ("acostarse", "ah-kohs-TAR-seh", "to go to bed", "Me acuesto temprano."),
    ("despertarse", "dehs-pehr-TAR-seh", "to wake up", "Me despierto con alarma."),
    ("ducharse", "doo-CHAR-seh", "to shower", "Me ducho por la manana."),
    ("banarse", "bah-NYAR-seh", "to bathe", "Me bano en la noche."),
    ("vestirse", "behs-TEER-seh", "to get dressed", "Me visto rapidamente."),
    ("peinarse", "peh-ee-NAR-seh", "to comb one's hair", "Me peino antes de salir."),
    ("lavarse", "lah-BAR-seh", "to wash oneself", "Me lavo las manos."),
    ("cepillarse", "seh-pee-YAR-seh", "to brush", "Me cepillo los dientes."),
    ("maquillarse", "mah-kee-YAR-seh", "to put on makeup", "Me maquillo poco."),
    ("afeitarse", "ah-feh-ee-TAR-seh", "to shave", "Me afeito cada dia."),
    ("sentarse", "sehn-TAR-seh", "to sit down", "Me siento en el sofa."),
    ("quedarse", "keh-DAR-seh", "to stay", "Me quedo en casa hoy."),
    ("irse", "EER-seh", "to leave/go away", "Me voy a las cinco."),
    ("llamarse", "yah-MAR-seh", "to be called", "Me llamo Carlos."),
    ("sentirse", "sehn-TEER-seh", "to feel", "Me siento feliz."),
    ("ponerse", "poh-NEHR-seh", "to put on/become", "Me pongo nervioso."),
    ("quitarse", "kee-TAR-seh", "to take off", "Me quito los zapatos."),
    ("casarse", "kah-SAR-seh", "to get married", "Se casan en junio."),
    ("divorciarse", "dee-bohr-see-AR-seh", "to get divorced", "Se divorciaron el ano pasado."),
    ("mudarse", "moo-DAR-seh", "to move (house)", "Me mudo a otra ciudad."),
    ("quejarse", "keh-HAR-seh", "to complain", "Se queja de todo."),
    ("olvidarse", "ohl-bee-DAR-seh", "to forget", "Me olvide de la cita."),
    ("acordarse", "ah-kohr-DAR-seh", "to remember", "Me acuerdo de ti."),
    ("comprometerse", "kohm-proh-meh-TEHR-seh", "to commit oneself", "Me comprometo a estudiar."),
    ("enterarse", "ehn-teh-RAR-seh", "to find out", "Me entere de la noticia."),
    ("darse cuenta", "DAR-seh KWEHN-tah", "to realize", "Me di cuenta del error."),
    ("equivocarse", "eh-kee-boh-KAR-seh", "to be wrong/make mistake", "Todos nos equivocamos."),
    ("preocuparse", "preh-oh-koo-PAR-seh", "to worry", "No te preocupes por eso."),
    ("arrepentirse", "ah-reh-pehn-TEER-seh", "to regret", "Me arrepiento de esa decision."),
    ("atreverse", "ah-treh-BEHR-seh", "to dare", "Me atrevo a intentarlo."),
    ("quemarse", "keh-MAR-seh", "to burn oneself", "Me queme con el cafe."),
    ("cortarse", "kohr-TAR-seh", "to cut oneself", "Me corte el dedo."),
    ("caerse", "kah-EHR-seh", "to fall down", "Me cai en la escalera."),
    ("romperse", "rohm-PEHR-seh", "to break", "Se rompio el vaso."),
    ("perderse", "pehr-DEHR-seh", "to get lost", "Me perdi en la ciudad."),
    ("aburrirse", "ah-boo-REER-seh", "to get bored", "Me aburro facilmente."),
    ("divertirse", "dee-behr-TEER-seh", "to have fun", "Me divierto mucho."),
    ("enojarse", "eh-noh-HAR-seh", "to get angry", "Me enojo rapidamente."),
    ("calmarse", "kahl-MAR-seh", "to calm down", "Calmate, por favor."),
    ("relajarse", "reh-lah-HAR-seh", "to relax", "Me relajo los fines de semana."),
    ("concentrarse", "kohn-sehn-TRAR-seh", "to concentrate", "Me concentro en el trabajo."),
    ("dedicarse", "deh-dee-KAR-seh", "to dedicate oneself", "Me dedico a la musica."),
    ("acercarse", "ah-sehr-KAR-seh", "to approach", "Me acerco a la puerta."),
    ("alejarse", "ah-leh-HAR-seh", "to move away", "Me alejo del ruido."),

    # ═══════════════════════════════════════════════════════════════════
    # ADJECTIVES (Beginner)
    # ═══════════════════════════════════════════════════════════════════
    ("grande", "GRAHN-deh", "big/large", "Es una casa grande."),
    ("pequeno", "peh-KEH-nyoh", "small", "Tengo un perro pequeno."),
    ("bueno", "BWEH-noh", "good", "Es un buen libro."),
    ("malo", "MAH-loh", "bad", "Hace mal tiempo hoy."),
    ("nuevo", "NWEH-boh", "new", "Compre un carro nuevo."),
    ("viejo", "bee-EH-hoh", "old", "Mi abuelo es viejo."),
    ("joven", "HOH-behn", "young", "Es una mujer joven."),
    ("alto", "AHL-toh", "tall/high", "El edificio es muy alto."),
    ("bajo", "BAH-hoh", "short/low", "El precio esta bajo."),
    ("largo", "LAR-goh", "long", "Es un camino largo."),
    ("corto", "KOHR-toh", "short", "Fue una pelicula corta."),
    ("ancho", "AHN-choh", "wide", "El rio es muy ancho."),
    ("estrecho", "ehs-TREH-choh", "narrow", "La calle es estrecha."),
    ("gordo", "GOHR-doh", "fat", "El gato esta gordo."),
    ("delgado", "dehl-GAH-doh", "thin", "Es muy delgado."),
    ("fuerte", "FWEHR-teh", "strong", "Es un hombre fuerte."),
    ("debil", "DEH-beel", "weak", "Me siento debil hoy."),
    ("rapido", "RAH-pee-doh", "fast", "El tren es muy rapido."),
    ("lento", "LEHN-toh", "slow", "El trafico esta lento."),
    ("facil", "FAH-seel", "easy", "El examen fue facil."),
    ("dificil", "dee-FEE-seel", "difficult", "Es un problema dificil."),
    ("rico", "REE-koh", "rich/delicious", "La comida esta rica."),
    ("pobre", "POH-breh", "poor", "Es una familia pobre."),
    ("caro", "KAH-roh", "expensive", "El hotel es muy caro."),
    ("barato", "bah-RAH-toh", "cheap", "Encontre algo barato."),
    ("limpio", "LEEM-pyoh", "clean", "La casa esta limpia."),
    ("sucio", "SOO-syoh", "dirty", "El piso esta sucio."),
    ("caliente", "kah-LYEHN-teh", "hot", "El cafe esta caliente."),
    ("frio", "FREE-oh", "cold", "El agua esta fria."),
    ("dulce", "DOOL-seh", "sweet", "El pastel es muy dulce."),
    ("salado", "sah-LAH-doh", "salty", "La sopa esta salada."),
    ("amargo", "ah-MAR-goh", "bitter", "El cafe es amargo."),
    ("picante", "pee-KAHN-teh", "spicy", "Me gusta la comida picante."),
    ("feliz", "feh-LEES", "happy", "Estoy muy feliz."),
    ("triste", "TREES-teh", "sad", "Se siente triste hoy."),
    ("cansado", "kahn-SAH-doh", "tired", "Estoy muy cansado."),
    ("enfermo", "ehn-FEHR-moh", "sick", "Mi hijo esta enfermo."),
    ("sano", "SAH-noh", "healthy", "Es importante estar sano."),
    ("ocupado", "oh-koo-PAH-doh", "busy", "Estoy muy ocupado."),
    ("libre", "LEE-breh", "free", "Estas libre manana?"),
    ("lleno", "YEH-noh", "full", "El restaurante esta lleno."),
    ("vacio", "bah-SEE-oh", "empty", "El vaso esta vacio."),
    ("abierto", "ah-BYEHR-toh", "open", "La tienda esta abierta."),
    ("cerrado", "seh-RAH-doh", "closed", "El banco esta cerrado."),
    ("correcto", "koh-REHK-toh", "correct", "Tu respuesta es correcta."),
    ("incorrecto", "een-koh-REHK-toh", "incorrect", "Eso es incorrecto."),
    ("seguro", "seh-GOO-roh", "safe/sure", "Estoy seguro de eso."),
    ("peligroso", "peh-lee-GROH-soh", "dangerous", "Es un lugar peligroso."),
    ("importante", "eem-pohr-TAHN-teh", "important", "Es muy importante."),
    ("interesante", "een-teh-reh-SAHN-teh", "interesting", "El libro es interesante."),

    # ═══════════════════════════════════════════════════════════════════
    # ADJECTIVES (Intermediate)
    # ═══════════════════════════════════════════════════════════════════
    ("disponible", "dees-poh-NEE-bleh", "available", "El doctor esta disponible manana."),
    ("imprescindible", "eem-prehs-seen-DEE-bleh", "essential/indispensable", "El agua es imprescindible."),
    ("agotado", "ah-goh-TAH-doh", "exhausted/sold out", "Estoy agotado despues del trabajo."),
    ("capaz", "kah-PAHS", "capable", "Eres capaz de hacerlo."),
    ("dispuesto", "dees-PWEHS-toh", "willing/ready", "Estoy dispuesto a ayudar."),
    ("actual", "ahk-TWAHL", "current (not actual!)", "La situacion actual es dificil."),
    ("adecuado", "ah-deh-KWAH-doh", "adequate/suitable", "Es el momento adecuado."),
    ("amplio", "AHM-plyoh", "wide/spacious", "Tiene un amplio conocimiento."),
    ("complicado", "kohm-plee-KAH-doh", "complicated", "Es un tema complicado."),
    ("sencillo", "sehn-SEE-yoh", "simple", "La solucion es sencilla."),
    ("profundo", "proh-FOON-doh", "deep", "Es un pensamiento profundo."),
    ("superficial", "soo-pehr-fee-SYAHL", "superficial", "Fue un analisis superficial."),
    ("valioso", "bah-LYOH-soh", "valuable", "Es un objeto muy valioso."),
    ("util", "OO-teel", "useful", "Es una herramienta util."),
    ("inutil", "ee-NOO-teel", "useless", "Es completamente inutil."),
    ("obvio", "OHB-byoh", "obvious", "Es algo obvio."),
    ("curioso", "koo-RYOH-soh", "curious", "Soy muy curioso."),
    ("gracioso", "grah-SYOH-soh", "funny", "Es muy gracioso."),
    ("serio", "SEH-ryoh", "serious", "Es un asunto serio."),
    ("tranquilo", "trahn-KEE-loh", "calm/quiet", "El lugar es muy tranquilo."),
    ("ruidoso", "rrwee-DOH-soh", "noisy", "La calle es ruidosa."),
    ("orgulloso", "ohr-goo-YOH-soh", "proud", "Estoy orgulloso de ti."),
    ("avergonzado", "ah-behr-gohn-SAH-doh", "embarrassed", "Estoy avergonzado."),
    ("emocionado", "eh-moh-syoh-NAH-doh", "excited", "Estoy emocionado por el viaje."),
    ("preocupado", "preh-oh-koo-PAH-doh", "worried", "Estoy preocupado por el examen."),
    ("sorprendido", "sohr-prehn-DEE-doh", "surprised", "Estoy muy sorprendido."),
    ("confundido", "kohn-foon-DEE-doh", "confused", "Estoy confundido."),
    ("decepcionado", "deh-sehp-syoh-NAH-doh", "disappointed", "Estoy decepcionado."),
    ("asustado", "ah-soos-TAH-doh", "scared", "El nino esta asustado."),
    ("celoso", "seh-LOH-soh", "jealous", "Es una persona celosa."),
    ("terco", "TEHR-koh", "stubborn", "Es muy terco."),
    ("timido", "TEE-mee-doh", "shy", "Mi hijo es timido."),
    ("amable", "ah-MAH-bleh", "kind", "Es una persona amable."),
    ("grosero", "groh-SEH-roh", "rude", "No seas grosero."),
    ("educado", "eh-doo-KAH-doh", "polite", "Es muy educado."),
    ("honesto", "oh-NEHS-toh", "honest", "Es una persona honesta."),
    ("sincero", "seen-SEH-roh", "sincere", "Fue un comentario sincero."),
    ("leal", "leh-AHL", "loyal", "Es un amigo leal."),
    ("fiel", "fyehl", "faithful", "Es un perro fiel."),
    ("generoso", "heh-neh-ROH-soh", "generous", "Es muy generoso."),
    ("tacano", "tah-KAH-nyoh", "stingy", "No seas tacano."),
    ("perezoso", "peh-reh-SOH-soh", "lazy", "Soy un poco perezoso."),
    ("trabajador", "trah-bah-hah-DOHR", "hardworking", "Es muy trabajador."),
    ("inteligente", "een-teh-lee-HEHN-teh", "intelligent", "Es muy inteligente."),
    ("listo", "LEES-toh", "smart/ready", "Estas listo para salir?"),
    ("tonto", "TOHN-toh", "silly/stupid", "No seas tonto."),
    ("sabio", "SAH-byoh", "wise", "Mi abuelo es muy sabio."),
    ("culpable", "kool-PAH-bleh", "guilty", "Se siente culpable."),
    ("inocente", "ee-noh-SEHN-teh", "innocent", "Es inocente."),

    # ═══════════════════════════════════════════════════════════════════
    # NOUNS (Common)
    # ═══════════════════════════════════════════════════════════════════
    ("desarrollo", "deh-sah-RROH-yoh", "development", "El desarrollo del proyecto va bien."),
    ("comportamiento", "kohm-pohr-tah-MYEHN-toh", "behavior", "Su comportamiento es extrano."),
    ("conocimiento", "koh-noh-see-MYEHN-toh", "knowledge", "El conocimiento es poder."),
    ("ambiente", "ahm-BYEHN-teh", "environment/atmosphere", "El ambiente es muy agradable."),
    ("herramienta", "eh-rrah-MYEHN-tah", "tool", "Necesito una herramienta."),
    ("recurso", "reh-KOOR-soh", "resource", "Tenemos pocos recursos."),
    ("prueba", "PRWEH-bah", "test/proof", "Necesito una prueba."),
    ("meta", "MEH-tah", "goal", "Mi meta es aprender espanol."),
    ("reto", "RREH-toh", "challenge", "Es un gran reto personal."),
    ("exito", "EHK-see-toh", "success", "El proyecto fue un exito."),
    ("fracaso", "frah-KAH-soh", "failure", "Aprende de tus fracasos."),
    ("ventaja", "behn-TAH-hah", "advantage", "Tenemos una gran ventaja."),
    ("desventaja", "dehs-behn-TAH-hah", "disadvantage", "Es una desventaja clara."),
    ("oportunidad", "oh-pohr-too-nee-DAHD", "opportunity", "Es una buena oportunidad."),
    ("posibilidad", "poh-see-bee-lee-DAHD", "possibility", "Existe esa posibilidad."),
    ("capacidad", "kah-pah-see-DAHD", "capacity/ability", "Tiene mucha capacidad."),
    ("habilidad", "ah-bee-lee-DAHD", "skill/ability", "Es una habilidad importante."),
    ("costumbre", "kohs-TOOM-breh", "custom/habit", "Es una vieja costumbre."),
    ("tradicion", "trah-dee-SYOHN", "tradition", "Es una tradicion familiar."),
    ("cultura", "kool-TOO-rah", "culture", "Me interesa la cultura mexicana."),
    ("historia", "ees-TOH-ryah", "history/story", "Cuenta una historia interesante."),
    ("cuenta", "KWEHN-tah", "account/bill", "Pago la cuenta, por favor."),
    ("cuento", "KWEHN-toh", "story/tale", "Leo un cuento a mis hijos."),
    ("noticia", "noh-TEE-syah", "news", "Es una buena noticia."),
    ("novedad", "noh-beh-DAHD", "novelty/news", "Que novedades hay?"),
    ("asunto", "ah-SOON-toh", "matter/issue", "Es un asunto importante."),
    ("tema", "TEH-mah", "topic/theme", "Cambiemos de tema."),
    ("problema", "proh-BLEH-mah", "problem", "Tengo un problema."),
    ("solucion", "soh-loo-SYOHN", "solution", "Busco una solucion."),
    ("respuesta", "rrehs-PWEHS-tah", "answer", "Necesito una respuesta."),
    ("pregunta", "preh-GOON-tah", "question", "Tengo una pregunta."),
    ("duda", "DOO-dah", "doubt", "No tengo ninguna duda."),
    ("error", "eh-RROHR", "error/mistake", "Cometio un error grave."),
    ("falta", "FAHL-tah", "lack/fault", "Es mi falta."),
    ("culpa", "KOOL-pah", "blame/fault", "No es tu culpa."),
    ("razon", "rrah-SOHN", "reason", "Tienes toda la razon."),
    ("motivo", "moh-TEE-boh", "motive/reason", "Cual es el motivo?"),
    ("causa", "KOW-sah", "cause", "Es la causa del problema."),
    ("efecto", "eh-FEHK-toh", "effect", "Tiene un efecto positivo."),
    ("resultado", "reh-sool-TAH-doh", "result", "El resultado fue bueno."),
    ("cambio", "KAHM-byoh", "change", "Necesito un cambio."),
    ("mejora", "meh-HOH-rah", "improvement", "Se nota la mejora."),
    ("aumento", "ow-MEHN-toh", "increase", "Hubo un aumento de precios."),
    ("disminucion", "dees-mee-noo-SYOHN", "decrease", "Hay una disminucion notable."),
    ("crecimiento", "kreh-see-MYEHN-toh", "growth", "El crecimiento es rapido."),
    ("nivel", "nee-BEHL", "level", "Subio de nivel."),
    ("grado", "GRAH-doh", "degree/grade", "Tiene un alto grado de dificultad."),
    ("medida", "meh-DEE-dah", "measure", "Tomo medidas urgentes."),
    ("paso", "PAH-soh", "step", "Es un paso importante."),
    ("etapa", "eh-TAH-pah", "stage/phase", "Es la primera etapa."),

    # ═══════════════════════════════════════════════════════════════════
    # EXPRESSIONS AND PHRASES
    # ═══════════════════════════════════════════════════════════════════
    ("sin embargo", "seen ehm-BAR-goh", "however/nevertheless", "Es dificil, sin embargo posible."),
    ("a pesar de", "ah peh-SAR deh", "despite/in spite of", "A pesar de todo, sigo adelante."),
    ("de repente", "deh reh-PEHN-teh", "suddenly", "De repente, empezo a llover."),
    ("hoy en dia", "oy ehn DEE-ah", "nowadays", "Hoy en dia todo es digital."),
    ("cada vez mas", "KAH-dah behs mahs", "more and more", "Es cada vez mas dificil."),
    ("hace poco", "AH-seh POH-koh", "a little while ago", "Llegue hace poco."),
    ("a la larga", "ah lah LAR-gah", "in the long run", "A la larga, vale la pena."),
    ("de antemano", "deh ahn-teh-MAH-noh", "beforehand", "Gracias de antemano."),
    ("en cuanto a", "ehn KWAHN-toh ah", "as for/regarding", "En cuanto a eso, no se."),
    ("por lo tanto", "pohr loh TAHN-toh", "therefore", "Por lo tanto, debemos actuar."),
    ("por lo menos", "pohr loh MEH-nohs", "at least", "Por lo menos lo intentaste."),
    ("por supuesto", "pohr soo-PWEHS-toh", "of course", "Por supuesto que si."),
    ("desde luego", "DEHS-deh LWEH-goh", "of course", "Desde luego que puedo."),
    ("en realidad", "ehn reh-ah-lee-DAHD", "actually/in reality", "En realidad, no es asi."),
    ("de hecho", "deh EH-choh", "in fact", "De hecho, tengo razon."),
    ("en cambio", "ehn KAHM-byoh", "on the other hand", "En cambio, el prefiere cafe."),
    ("en vez de", "ehn behs deh", "instead of", "En vez de quejarme, actuare."),
    ("a menudo", "ah meh-NOO-doh", "often", "Voy a menudo al cine."),
    ("de vez en cuando", "deh behs ehn KWAHN-doh", "from time to time", "Como pizza de vez en cuando."),
    ("poco a poco", "POH-koh ah POH-koh", "little by little", "Aprendo poco a poco."),
    ("al principio", "ahl preen-SEE-pyoh", "at the beginning", "Al principio fue dificil."),
    ("al final", "ahl fee-NAHL", "in the end", "Al final todo salio bien."),
    ("por fin", "pohr feen", "finally", "Por fin llegamos!"),
    ("mientras tanto", "MYEHN-trahs TAHN-toh", "meanwhile", "Mientras tanto, espera aqui."),
    ("en seguida", "ehn seh-GEE-dah", "right away", "Vengo en seguida."),
    ("ahora mismo", "ah-OH-rah MEES-moh", "right now", "Lo hago ahora mismo."),
    ("de pronto", "deh PROHN-toh", "suddenly", "De pronto, sono el telefono."),
    ("de todos modos", "deh TOH-dohs MOH-dohs", "anyway", "De todos modos, gracias."),
    ("en serio", "ehn SEH-ryoh", "seriously", "Hablas en serio?"),
    ("tal vez", "tahl behs", "maybe/perhaps", "Tal vez venga manana."),
    ("quizas", "kee-SAHS", "perhaps", "Quizas tengas razon."),
    ("a lo mejor", "ah loh meh-HOHR", "maybe/probably", "A lo mejor llueve hoy."),
    ("sobre todo", "SOH-breh TOH-doh", "especially", "Me gusta, sobre todo el final."),
    ("mas o menos", "mahs oh MEH-nohs", "more or less", "Mas o menos entiendo."),
    ("ni siquiera", "nee see-KYEH-rah", "not even", "Ni siquiera me miro."),
    ("en absoluto", "ehn ahb-soh-LOO-toh", "absolutely not", "En absoluto, no es verdad."),
    ("ya que", "yah keh", "since/given that", "Ya que estas aqui, ayudame."),
    ("puesto que", "PWEHS-toh keh", "since/given that", "Puesto que insistes, acepto."),
    ("siempre y cuando", "SYEHM-preh ee KWAHN-doh", "as long as", "Siempre y cuando me avises."),
    ("con tal de que", "kohn tahl deh keh", "provided that", "Con tal de que llegues a tiempo."),
    ("a menos que", "ah MEH-nohs keh", "unless", "A menos que cambie de opinion."),
    ("en caso de que", "ehn KAH-soh deh keh", "in case", "En caso de que llueva."),
    ("hasta que", "AHS-tah keh", "until", "Espera hasta que llegue."),
    ("antes de que", "AHN-tehs deh keh", "before", "Llamame antes de que salgas."),
    ("despues de que", "dehs-PWEHS deh keh", "after", "Despues de que termine."),
    ("para que", "PAH-rah keh", "so that", "Te lo digo para que sepas."),
    ("a fin de que", "ah feen deh keh", "in order that", "A fin de que entiendas."),
    ("con el fin de", "kohn ehl feen deh", "in order to", "Con el fin de mejorar."),
    ("a causa de", "ah KOW-sah deh", "because of", "A causa de la lluvia."),
    ("debido a", "deh-BEE-doh ah", "due to", "Debido a problemas tecnicos."),
    ("gracias a", "GRAH-syahs ah", "thanks to", "Gracias a ti, lo logre."),

    # ═══════════════════════════════════════════════════════════════════
    # FALSE FRIENDS (Cognates that don't mean what you think)
    # ═══════════════════════════════════════════════════════════════════
    ("embarazada", "ehm-bah-rah-SAH-dah", "pregnant (not embarrassed!)", "Mi hermana esta embarazada."),
    ("exito", "EHK-see-toh", "success (not exit!)", "El proyecto fue un exito."),
    ("sensible", "sehn-SEE-bleh", "sensitive (not sensible!)", "Es una persona muy sensible."),
    ("libreria", "lee-breh-REE-ah", "bookstore (not library!)", "Compre un libro en la libreria."),
    ("recordar", "reh-kohr-DAR", "to remember (not record!)", "Recuerdo ese dia."),
    ("actual", "ahk-TWAHL", "current (not actual!)", "La situacion actual es dificil."),
    ("asistir", "ah-sees-TEER", "to attend (not assist!)", "Asisto a clases de yoga."),
    ("realizar", "reh-ah-lee-SAR", "to accomplish (not realize!)", "Realice mi sueno."),
    ("contestar", "kohn-tehs-TAR", "to answer (not contest!)", "Contesta el telefono."),
    ("pretender", "preh-tehn-DEHR", "to try (not pretend!)", "Pretendo mejorar."),
    ("soportar", "soh-pohr-TAR", "to tolerate (not support!)", "No soporto el ruido."),
    ("molestar", "moh-lehs-TAR", "to bother (not molest!)", "No me molestes ahora."),
    ("introducir", "een-troh-doo-SEER", "to insert (not introduce!)", "Introduce la tarjeta."),
    ("fabrica", "FAH-bree-kah", "factory (not fabric!)", "Trabaja en una fabrica."),
    ("carpeta", "kar-PEH-tah", "folder (not carpet!)", "Pon los papeles en la carpeta."),
    ("campo", "KAHM-poh", "field/countryside (not camp!)", "Vive en el campo."),
    ("casualidad", "kah-swah-lee-DAHD", "coincidence (not casualty!)", "Que casualidad!"),
    ("compromiso", "kohm-proh-MEE-soh", "commitment (not compromise!)", "Es un gran compromiso."),
    ("constipado", "kohns-tee-PAH-doh", "having a cold (not constipated!)", "Estoy constipado."),
    ("decepcion", "deh-sehp-SYOHN", "disappointment (not deception!)", "Fue una gran decepcion."),
    ("delito", "deh-LEE-toh", "crime (not delight!)", "Cometio un delito grave."),
    ("disgusto", "dees-GOOS-toh", "upset/annoyance (not disgust!)", "Me dio un disgusto."),
    ("diversion", "dee-behr-SYOHN", "fun (not diversion!)", "Busco diversion."),
    ("embarazar", "ehm-bah-rah-SAR", "to impregnate (not embarrass!)", "La embarazo sin querer."),
    ("emocionante", "eh-moh-syoh-NAHN-teh", "exciting (not emotional!)", "Fue un partido emocionante."),
    ("equipaje", "eh-kee-PAH-heh", "luggage (not equipment!)", "Perdieron mi equipaje."),
    ("eventual", "eh-behn-TWAHL", "possible (not eventual!)", "Es un resultado eventual."),
    ("largo", "LAR-goh", "long (not large!)", "Es un camino largo."),
    ("lectura", "lehk-TOO-rah", "reading (not lecture!)", "Me gusta la lectura."),
    ("parientes", "pah-RYEHN-tehs", "relatives (not parents!)", "Mis parientes vienen de visita."),
    ("propaganda", "proh-pah-GAHN-dah", "advertising (not propaganda!)", "Hay mucha propaganda en la tele."),
    ("ropa", "RROH-pah", "clothes (not rope!)", "Compre ropa nueva."),
    ("suceso", "soo-SEH-soh", "event (not success!)", "Fue un suceso importante."),
    ("simpatico", "seem-PAH-tee-koh", "nice/likeable (not sympathetic!)", "Es muy simpatico."),
    ("tuna", "TOO-nah", "prickly pear (not tuna fish!)", "Me gusta la tuna."),

    # ═══════════════════════════════════════════════════════════════════
    # SUBJUNCTIVE TRIGGERS
    # ═══════════════════════════════════════════════════════════════════
    ("es importante que", "ehs eem-pohr-TAHN-teh keh", "it's important that", "Es importante que estudies."),
    ("espero que", "ehs-PEH-roh keh", "I hope that", "Espero que todo salga bien."),
    ("ojala", "oh-hah-LAH", "hopefully/I wish", "Ojala puedas venir."),
    ("aunque", "OWN-keh", "although/even if", "Voy aunque llueva."),
    ("dudo que", "DOO-doh keh", "I doubt that", "Dudo que venga."),
    ("no creo que", "noh KREH-oh keh", "I don't think that", "No creo que sea verdad."),
    ("es posible que", "ehs poh-SEE-bleh keh", "it's possible that", "Es posible que llueva."),
    ("es probable que", "ehs proh-BAH-bleh keh", "it's probable that", "Es probable que gane."),
    ("es necesario que", "ehs neh-seh-SAH-ryoh keh", "it's necessary that", "Es necesario que trabajes."),
    ("quiero que", "KYEH-roh keh", "I want that", "Quiero que vengas."),
    ("necesito que", "neh-seh-SEE-toh keh", "I need that", "Necesito que me ayudes."),
    ("te pido que", "teh PEE-doh keh", "I ask you to", "Te pido que me escuches."),
    ("me alegra que", "meh ah-LEH-grah keh", "I'm glad that", "Me alegra que estes aqui."),
    ("siento que", "SYEHN-toh keh", "I'm sorry that", "Siento que te vayas."),
    ("temo que", "TEH-moh keh", "I fear that", "Temo que sea tarde."),
    ("cuando", "KWAHN-doh", "when (future)", "Cuando llegues, llamame."),
    ("en cuanto", "ehn KWAHN-toh", "as soon as", "En cuanto pueda, te aviso."),
    ("tan pronto como", "tahn PROHN-toh KOH-moh", "as soon as", "Tan pronto como termine."),
    ("despues de que", "dehs-PWEHS deh keh", "after", "Despues de que comas."),
    ("antes de que", "AHN-tehs deh keh", "before", "Antes de que te vayas."),
    ("hasta que", "AHS-tah keh", "until", "Espera hasta que vuelva."),
    ("mientras", "MYEHN-trahs", "while", "Mientras estudies, mejoras."),
    ("a menos que", "ah MEH-nohs keh", "unless", "A menos que cambies de idea."),
    ("con tal de que", "kohn tahl deh keh", "provided that", "Con tal de que lo intentes."),
    ("para que", "PAH-rah keh", "so that", "Lo hago para que entiendas."),
    ("sin que", "seen keh", "without", "Salio sin que lo viera."),

    # ═══════════════════════════════════════════════════════════════════
    # TIME EXPRESSIONS
    # ═══════════════════════════════════════════════════════════════════
    ("hace poco", "AH-seh POH-koh", "a little while ago", "Llegue hace poco."),
    ("dentro de poco", "DEHN-troh deh POH-koh", "in a little while", "Llego dentro de poco."),
    ("a la larga", "ah lah LAR-gah", "in the long run", "A la larga, vale la pena."),
    ("de antemano", "deh ahn-teh-MAH-noh", "beforehand", "Gracias de antemano."),
    ("hace mucho", "AH-seh MOO-choh", "a long time ago", "Fue hace mucho tiempo."),
    ("en aquel entonces", "ehn ah-KEHL ehn-TOHN-sehs", "back then", "En aquel entonces, era diferente."),
    ("hoy en dia", "oy ehn DEE-ah", "nowadays", "Hoy en dia es comun."),
    ("ultimamente", "OOL-tee-mah-MEHN-teh", "lately", "Ultimamente trabajo mucho."),
    ("recientemente", "reh-syehn-teh-MEHN-teh", "recently", "Recientemente me mude."),
    ("antiguamente", "ahn-tee-gwah-MEHN-teh", "in the old days", "Antiguamente no habia internet."),
    ("en el futuro", "ehn ehl foo-TOO-roh", "in the future", "En el futuro sera mejor."),
    ("a partir de ahora", "ah par-TEER deh ah-OH-rah", "from now on", "A partir de ahora, cambio."),
    ("de ahora en adelante", "deh ah-OH-rah ehn ah-deh-LAHN-teh", "from now on", "De ahora en adelante, sere puntual."),
    ("a primera hora", "ah pree-MEH-rah OH-rah", "first thing", "Te llamo a primera hora."),
    ("a ultima hora", "ah OOL-tee-mah OH-rah", "at the last minute", "Cancelo a ultima hora."),
    ("cuanto antes", "KWAHN-toh AHN-tehs", "as soon as possible", "Hazlo cuanto antes."),
    ("lo antes posible", "loh AHN-tehs poh-SEE-bleh", "as soon as possible", "Necesito verlo lo antes posible."),
    ("de vez en cuando", "deh behs ehn KWAHN-doh", "from time to time", "Voy de vez en cuando."),
    ("a veces", "ah BEH-sehs", "sometimes", "A veces me olvido."),
    ("siempre", "SYEHM-preh", "always", "Siempre llego temprano."),
    ("nunca", "NOON-kah", "never", "Nunca me rindo."),
    ("jamas", "hah-MAHS", "never (emphatic)", "Jamas lo olvidare."),
    ("ya", "yah", "already/now", "Ya termine."),
    ("todavia", "toh-dah-BEE-ah", "still/yet", "Todavia no llega."),
    ("aun", "ah-OON", "still/even", "Aun no lo se."),

    # ═══════════════════════════════════════════════════════════════════
    # QUESTION WORDS AND PHRASES
    # ═══════════════════════════════════════════════════════════════════
    ("que", "keh", "what/which", "Que quieres comer?"),
    ("quien", "kyehn", "who", "Quien es esa persona?"),
    ("quienes", "KYEH-nehs", "who (plural)", "Quienes vienen a la fiesta?"),
    ("donde", "DOHN-deh", "where", "Donde esta el bano?"),
    ("adonde", "ah-DOHN-deh", "to where", "Adonde vas?"),
    ("de donde", "deh DOHN-deh", "from where", "De donde eres?"),
    ("cuando", "KWAHN-doh", "when", "Cuando es tu cumpleanos?"),
    ("desde cuando", "DEHS-deh KWAHN-doh", "since when", "Desde cuando vives aqui?"),
    ("hasta cuando", "AHS-tah KWAHN-doh", "until when", "Hasta cuando te quedas?"),
    ("por que", "pohr KEH", "why", "Por que no viniste?"),
    ("para que", "PAH-rah keh", "what for", "Para que sirve esto?"),
    ("como", "KOH-moh", "how", "Como estas?"),
    ("cuanto", "KWAHN-toh", "how much", "Cuanto cuesta?"),
    ("cuantos", "KWAHN-tohs", "how many", "Cuantos anos tienes?"),
    ("cual", "kwahl", "which/what", "Cual prefieres?"),
    ("cuales", "KWAH-lehs", "which ones", "Cuales son tuyos?"),

    # ═══════════════════════════════════════════════════════════════════
    # CONNECTING WORDS
    # ═══════════════════════════════════════════════════════════════════
    ("y", "ee", "and", "Pan y mantequilla."),
    ("e", "eh", "and (before i/hi)", "Madre e hija."),
    ("o", "oh", "or", "Te o cafe?"),
    ("u", "oo", "or (before o/ho)", "Siete u ocho."),
    ("pero", "PEH-roh", "but", "Quiero ir, pero no puedo."),
    ("sino", "SEE-noh", "but rather", "No es azul, sino verde."),
    ("aunque", "OWN-keh", "although", "Aunque llueva, voy."),
    ("porque", "POHR-keh", "because", "Lo hago porque quiero."),
    ("ya que", "yah keh", "since", "Ya que insistes, acepto."),
    ("como", "KOH-moh", "since/as", "Como no llamaste, me fui."),
    ("por eso", "pohr EH-soh", "that's why", "Por eso llegue tarde."),
    ("asi que", "ah-SEE keh", "so", "Asi que eso paso."),
    ("entonces", "ehn-TOHN-sehs", "then/so", "Entonces que hacemos?"),
    ("ademas", "ah-deh-MAHS", "besides/moreover", "Ademas, es mas barato."),
    ("tambien", "tahm-BYEHN", "also", "Yo tambien quiero ir."),
    ("tampoco", "tahm-POH-koh", "neither", "Yo tampoco lo se."),
    ("ni", "nee", "nor/not even", "Ni lo intentes."),
    ("ni siquiera", "nee see-KYEH-rah", "not even", "Ni siquiera lo miro."),
    ("sin embargo", "seen ehm-BAR-goh", "however", "Sin embargo, lo intentare."),
    ("no obstante", "noh ohbs-TAHN-teh", "nevertheless", "No obstante, sigo adelante."),
    ("en cambio", "ehn KAHM-byoh", "on the other hand", "El, en cambio, prefiere te."),
    ("por un lado", "pohr oon LAH-doh", "on one hand", "Por un lado, es interesante."),
    ("por otro lado", "pohr OH-troh LAH-doh", "on the other hand", "Por otro lado, es caro."),
    ("es decir", "ehs deh-SEER", "that is to say", "Es decir, no funciona."),
    ("o sea", "oh SEH-ah", "in other words", "O sea, estas de acuerdo?"),
    ("por ejemplo", "pohr eh-HEHM-ploh", "for example", "Por ejemplo, el cafe."),

    # ═══════════════════════════════════════════════════════════════════
    # EVERYDAY PHRASES
    # ═══════════════════════════════════════════════════════════════════
    ("buenos dias", "BWEH-nohs DEE-ahs", "good morning", "Buenos dias, como estas?"),
    ("buenas tardes", "BWEH-nahs TAR-dehs", "good afternoon", "Buenas tardes a todos."),
    ("buenas noches", "BWEH-nahs NOH-chehs", "good evening/night", "Buenas noches, que descanses."),
    ("hola", "OH-lah", "hello", "Hola, que tal?"),
    ("adios", "ah-DYOHS", "goodbye", "Adios, hasta pronto!"),
    ("hasta luego", "AHS-tah LWEH-goh", "see you later", "Hasta luego, nos vemos."),
    ("hasta manana", "AHS-tah mah-NYAH-nah", "see you tomorrow", "Hasta manana en clase."),
    ("hasta pronto", "AHS-tah PROHN-toh", "see you soon", "Hasta pronto, amigo."),
    ("nos vemos", "nohs BEH-mohs", "see you", "Nos vemos el lunes."),
    ("que tal", "keh tahl", "how's it going", "Que tal tu dia?"),
    ("como estas", "KOH-moh ehs-TAHS", "how are you", "Como estas hoy?"),
    ("muy bien", "mwee byehn", "very well", "Estoy muy bien, gracias."),
    ("mas o menos", "mahs oh MEH-nohs", "so-so", "Mas o menos, tirando."),
    ("gracias", "GRAH-syahs", "thank you", "Muchas gracias por todo."),
    ("de nada", "deh NAH-dah", "you're welcome", "De nada, un placer."),
    ("por favor", "pohr fah-BOHR", "please", "Por favor, ayudame."),
    ("lo siento", "loh SYEHN-toh", "I'm sorry", "Lo siento mucho."),
    ("perdon", "pehr-DOHN", "excuse me/sorry", "Perdon, no te vi."),
    ("disculpe", "dees-KOOL-peh", "excuse me (formal)", "Disculpe, donde esta...?"),
    ("con permiso", "kohn pehr-MEE-soh", "excuse me (passing)", "Con permiso, necesito pasar."),
    ("salud", "sah-LOOD", "bless you/cheers", "Salud! Por tu exito."),
    ("felicidades", "feh-lee-see-DAH-dehs", "congratulations", "Felicidades por tu logro!"),
    ("buen provecho", "bwehn proh-BEH-choh", "enjoy your meal", "Buen provecho a todos."),
    ("que aproveche", "keh ah-proh-BEH-cheh", "enjoy your meal", "Que aproveche!"),
    ("que te mejores", "keh teh meh-HOH-rehs", "get well soon", "Que te mejores pronto."),
    ("buena suerte", "BWEH-nah SWEHR-teh", "good luck", "Buena suerte en el examen."),
    ("que te vaya bien", "keh teh BAH-yah byehn", "take care", "Que te vaya bien!"),
    ("cuidate", "kwee-DAH-teh", "take care", "Cuidate mucho."),
    ("igualmente", "ee-gwahl-MEHN-teh", "likewise", "Feliz ano! Igualmente."),
    ("con mucho gusto", "kohn MOO-choh GOOS-toh", "with pleasure", "Con mucho gusto te ayudo."),
    ("encantado", "ehn-kahn-TAH-doh", "pleased to meet you", "Encantado de conocerte."),
    ("mucho gusto", "MOO-choh GOOS-toh", "nice to meet you", "Mucho gusto, soy Ana."),
    ("como te llamas", "KOH-moh teh YAH-mahs", "what's your name", "Como te llamas?"),
    ("de donde eres", "deh DOHN-deh EH-rehs", "where are you from", "De donde eres?"),
    ("a que te dedicas", "ah keh teh deh-DEE-kahs", "what do you do", "A que te dedicas?"),
    ("en que trabajas", "ehn keh trah-BAH-hahs", "what's your job", "En que trabajas?"),
    ("que hora es", "keh OH-rah ehs", "what time is it", "Que hora es?"),
    ("que dia es hoy", "keh DEE-ah ehs oy", "what day is it", "Que dia es hoy?"),
    ("que tiempo hace", "keh TYEHM-poh AH-seh", "what's the weather", "Que tiempo hace hoy?"),
    ("cuanto cuesta", "KWAHN-toh KWEHS-tah", "how much does it cost", "Cuanto cuesta esto?"),
    ("me puede ayudar", "meh PWEH-deh ah-yoo-DAR", "can you help me", "Me puede ayudar?"),
    ("no entiendo", "noh ehn-TYEHN-doh", "I don't understand", "Lo siento, no entiendo."),
    ("puede repetir", "PWEH-deh reh-peh-TEER", "can you repeat", "Puede repetir, por favor?"),
    ("mas despacio", "mahs dehs-PAH-syoh", "more slowly", "Mas despacio, por favor."),
    ("como se dice", "KOH-moh seh DEE-seh", "how do you say", "Como se dice 'hello'?"),
    ("que significa", "keh seeg-nee-FEE-kah", "what does it mean", "Que significa esta palabra?"),
    ("no se", "noh seh", "I don't know", "No se la respuesta."),
    ("creo que si", "KREH-oh keh see", "I think so", "Creo que si."),
    ("creo que no", "KREH-oh keh noh", "I don't think so", "Creo que no."),
    ("claro", "KLAH-roh", "of course/sure", "Claro que si!"),
    ("vale", "BAH-leh", "okay (Spain)", "Vale, nos vemos luego."),
    ("dale", "DAH-leh", "okay (Latin America)", "Dale, hagamoslo."),
    ("esta bien", "ehs-TAH byehn", "it's okay", "Esta bien, no te preocupes."),
    ("no hay problema", "noh ay proh-BLEH-mah", "no problem", "No hay problema."),
    ("no pasa nada", "noh PAH-sah NAH-dah", "it's nothing", "No pasa nada, tranquilo."),
    ("en serio", "ehn SEH-ryoh", "seriously?", "En serio? No lo puedo creer."),
    ("de verdad", "deh behr-DAHD", "really?", "De verdad? Que bien!"),
    ("que bien", "keh byehn", "how nice!", "Que bien que llegaste!"),
    ("que bueno", "keh BWEH-noh", "that's great", "Que bueno que viniste!"),
    ("que lastima", "keh LAHS-tee-mah", "what a pity", "Que lastima que no puedas venir."),
    ("que pena", "keh PEH-nah", "what a shame", "Que pena que paso eso."),
    ("que horror", "keh oh-RROHR", "how awful", "Que horror! Que paso?"),
    ("que rollo", "keh RROH-yoh", "what a drag", "Que rollo tener que esperar."),
    ("que lio", "keh LEE-oh", "what a mess", "Que lio se armo!"),
    ("que barbaridad", "keh bar-bah-ree-DAHD", "how outrageous", "Que barbaridad!"),
    ("no me digas", "noh meh DEE-gahs", "you don't say", "No me digas! En serio?"),
]


class DRMAdBlocker:
    """
    DRM-based ad blocker using ustreamer's native blocking mode.

    Uses a simple GStreamer pipeline for display with queue element for smooth playback.
    All overlay compositing (background, preview, text) done in ustreamer's MPP encoder.
    """

    def __init__(self, connector_id=215, plane_id=72, minus_instance=None, ustreamer_port=9090,
                 output_width=1920, output_height=1080):
        self.is_visible = False
        self.current_source = None
        self.connector_id = connector_id
        self.plane_id = plane_id
        self.ustreamer_port = ustreamer_port
        self.minus = minus_instance
        self.output_width = output_width or 1920
        self.output_height = output_height or 1080
        self._lock = threading.Lock()

        # GStreamer pipeline
        self.pipeline = None
        self.bus = None

        # Audio passthrough reference
        self.audio = None

        # Pipeline health tracking
        self._pipeline_errors = 0
        self._last_error_time = 0
        self._pipeline_restarting = False
        self._restart_lock = threading.Lock()

        # FPS tracking
        self._frame_count = 0
        self._fps_start_time = time.time()
        self._current_fps = 0.0
        self._fps_lock = threading.Lock()

        # Video buffer watchdog
        self._last_buffer_time = 0
        self._watchdog_thread = None
        self._stop_watchdog = threading.Event()
        self._watchdog_interval = 3.0
        self._stall_threshold = 10.0
        self._restart_count = 0
        self._last_restart_time = 0
        self._consecutive_failures = 0
        self._base_restart_delay = 1.0
        self._max_restart_delay = 30.0
        self._success_reset_time = 10.0

        # Text rotation
        self._rotation_thread = None
        self._stop_rotation = threading.Event()

        # Debug overlay
        self._debug_overlay_enabled = True
        self._debug_thread = None
        self._stop_debug = threading.Event()
        self._debug_interval = 2.0
        self._total_blocking_time = 0.0
        self._current_block_start = None
        self._total_ads_blocked = 0

        # Preview settings - use actual capture resolution for positioning
        self._preview_enabled = True
        self._frame_width, self._frame_height = self._detect_frame_resolution()
        self._preview_w = int(self._frame_width * 0.20)
        self._preview_h = int(self._frame_height * 0.20)
        self._preview_padding = int(self._frame_height * 0.02)

        # Skip status
        self._skip_available = False
        self._skip_text = None

        # Time saved tracking
        self._total_time_saved = 0.0

        # Animation settings
        self._animation_thread = None
        self._stop_animation = threading.Event()
        self._animation_duration_start = 1.5
        self._animation_duration_end = 1.0
        self._animating = False
        self._animation_direction = None
        self._animation_source = None

        # Test mode
        self._test_blocking_until = 0

        # Snapshot buffer
        self._snapshot_buffer = deque(maxlen=3)
        self._snapshot_buffer_thread = None
        self._stop_snapshot_buffer = threading.Event()
        self._snapshot_interval = 2.0

        # Initialize GStreamer
        Gst.init(None)
        self._init_pipeline()
        self._start_snapshot_buffer()

    def _detect_frame_resolution(self):
        """Detect actual capture frame resolution from ustreamer."""
        try:
            url = f"http://localhost:{self.ustreamer_port}/state"
            with urllib.request.urlopen(url, timeout=2.0) as response:
                data = json.loads(response.read().decode('utf-8'))
                width = data.get('result', {}).get('source', {}).get('resolution', {}).get('width', 1920)
                height = data.get('result', {}).get('source', {}).get('resolution', {}).get('height', 1080)
                logger.info(f"[DRMAdBlocker] Detected frame resolution: {width}x{height}")
                return width, height
        except Exception as e:
            logger.warning(f"[DRMAdBlocker] Could not detect frame resolution: {e}, using 1920x1080")
            return 1920, 1080

    def _blocking_api_call(self, endpoint, params=None, data=None, method='GET', timeout=0.1):
        """Make an API call to ustreamer blocking endpoint."""
        try:
            url = f"http://localhost:{self.ustreamer_port}{endpoint}"
            if params:
                url += '?' + urllib.parse.urlencode(params)

            if method == 'POST' and data:
                req = urllib.request.Request(url, data=data, method='POST')
                req.add_header('Content-Type', 'image/jpeg')
            else:
                req = urllib.request.Request(url)

            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            logger.debug(f"[DRMAdBlocker] API call error ({endpoint}): {e}")
            return None

    def _init_pipeline(self):
        """Initialize simple GStreamer display pipeline with queue element."""
        try:
            # Simple pipeline with queue element to prevent buffer buildup
            pipeline_str = (
                f"souphttpsrc location=http://localhost:{self.ustreamer_port}/stream blocksize=524288 ! "
                f"multipartdemux ! jpegparse ! mppjpegdec ! video/x-raw,format=NV12 ! "
                f"videobalance saturation=0.85 name=colorbalance ! "
                f"queue max-size-buffers=3 leaky=downstream name=videoqueue ! "
                f"identity name=fpsprobe ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false"
            )

            logger.debug("[DRMAdBlocker] Creating pipeline with queue element...")
            self.pipeline = Gst.parse_launch(pipeline_str)

            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)
            self.bus.connect('message::eos', self._on_eos)
            self.bus.connect('message::warning', self._on_warning)

            fpsprobe = self.pipeline.get_by_name('fpsprobe')
            if fpsprobe:
                srcpad = fpsprobe.get_static_pad('src')
                srcpad.add_probe(Gst.PadProbeType.BUFFER, self._fps_probe_callback, None)

            logger.info("[DRMAdBlocker] Pipeline created (ustreamer blocking mode)")

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to initialize GStreamer: {e}")
            self.pipeline = None

    def _fps_probe_callback(self, pad, info, user_data):
        current_time = time.time()
        self._last_buffer_time = current_time

        if self._consecutive_failures > 0:
            if current_time - self._last_restart_time > self._success_reset_time:
                self._consecutive_failures = 0

        with self._fps_lock:
            self._frame_count += 1
            elapsed = current_time - self._fps_start_time
            if elapsed >= 1.0:
                self._current_fps = self._frame_count / elapsed
                self._frame_count = 0
                self._fps_start_time = current_time

        return Gst.PadProbeReturn.OK

    def get_fps(self):
        with self._fps_lock:
            return self._current_fps

    def start(self):
        # Stop any loading animation before starting normal pipeline
        self._stop_loading_animation()

        # If we're in loading or no-signal mode, need to reinitialize the normal pipeline
        if self.current_source in ('loading', 'no_hdmi_device'):
            logger.info(f"[DRMAdBlocker] Transitioning from {self.current_source} to normal pipeline")
            # Stop and destroy the standalone pipeline
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None
            # Reinitialize the normal pipeline
            self._init_pipeline()

        if not self.pipeline:
            logger.error("[DRMAdBlocker] No pipeline to start")
            return False

        try:
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start pipeline")
                return False

            logger.info("[DRMAdBlocker] Pipeline started")
            self._start_watchdog()

            # Re-detect frame resolution now that ustreamer should be running
            self._update_frame_resolution()

            # Clear loading state
            self.current_source = None
            self.is_visible = False

            return True

        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start pipeline: {e}")
            return False

    def _update_frame_resolution(self):
        """Update frame resolution and recalculate preview dimensions."""
        new_w, new_h = self._detect_frame_resolution()
        if new_w != self._frame_width or new_h != self._frame_height:
            self._frame_width = new_w
            self._frame_height = new_h
            self._preview_w = int(self._frame_width * 0.20)
            self._preview_h = int(self._frame_height * 0.20)
            self._preview_padding = int(self._frame_height * 0.02)
            logger.info(f"[DRMAdBlocker] Updated preview size to {self._preview_w}x{self._preview_h}")

    def _start_watchdog(self):
        self._stop_watchdog.clear()
        self._last_buffer_time = time.time()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="VideoWatchdog")
        self._watchdog_thread.start()

    def _stop_watchdog_thread(self):
        self._stop_watchdog.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

    def _watchdog_loop(self):
        while not self._stop_watchdog.is_set():
            self._stop_watchdog.wait(self._watchdog_interval)
            if self._stop_watchdog.is_set():
                break
            if self._pipeline_restarting:
                continue
            if self._last_buffer_time > 0:
                time_since_buffer = time.time() - self._last_buffer_time
                if time_since_buffer > self._stall_threshold:
                    logger.warning(f"[DRMAdBlocker] Pipeline stalled ({time_since_buffer:.1f}s)")
                    self._restart_pipeline()
            if self.pipeline:
                try:
                    state_ret, state, pending = self.pipeline.get_state(0)
                    if state not in (Gst.State.PLAYING, Gst.State.PAUSED):
                        self._restart_pipeline()
                except Exception:
                    pass

    def _restart_pipeline(self):
        with self._restart_lock:
            if self._pipeline_restarting:
                return
            self._pipeline_restarting = True

        try:
            self._restart_count += 1
            self._consecutive_failures += 1
            delay = min(self._base_restart_delay * (2 ** (self._consecutive_failures - 1)), self._max_restart_delay)
            logger.warning(f"[DRMAdBlocker] Restarting pipeline (attempt {self._restart_count}, delay {delay:.1f}s)")

            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None

            time.sleep(delay)
            self._init_pipeline()

            if self.pipeline:
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                if ret != Gst.StateChangeReturn.FAILURE:
                    logger.info("[DRMAdBlocker] Pipeline restarted successfully")
                    self._last_buffer_time = time.time()
                    self._last_restart_time = time.time()
        finally:
            self._pipeline_restarting = False

    def restart(self):
        logger.info("[DRMAdBlocker] External restart requested")
        threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def start_no_signal_mode(self):
        """Start a standalone display for 'No HDMI Input' message.

        This creates a simple pipeline using videotestsrc that doesn't depend on ustreamer.
        """
        try:
            # Stop any existing loading animation
            self._stop_loading_animation()

            # Stop existing pipeline if any
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None

            # Create a simple standalone pipeline for no-signal display
            # Uses videotestsrc with textoverlay - doesn't need ustreamer
            no_signal_pipeline = (
                f"videotestsrc pattern=black ! "
                f"video/x-raw,width=1920,height=1080,framerate=30/1 ! "
                f"textoverlay text=\"NO HDMI INPUT\" "
                f"valignment=center halignment=center font-desc=\"Sans Bold 24\" ! "
                f"videoconvert ! video/x-raw,format=NV12 ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false"
            )

            logger.debug("[DRMAdBlocker] Creating no-signal pipeline...")
            self.pipeline = Gst.parse_launch(no_signal_pipeline)

            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start no-signal pipeline")
                return False

            self.is_visible = True
            self.current_source = 'no_hdmi_device'
            logger.info("[DRMAdBlocker] No-signal display started")
            return True
        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start no-signal mode: {e}")
            return False

    def start_loading_mode(self):
        """Start a standalone display for 'Loading' with animated ellipses.

        This creates a pipeline using videotestsrc that shows "Loading" with
        animated dots (0-4 dots, increasing then decreasing).
        """
        try:
            # Stop any existing loading animation
            self._stop_loading_animation()

            # Stop existing pipeline if any
            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self.pipeline = None

            # Create a standalone pipeline for loading display
            # Uses videotestsrc with named textoverlay for animation
            loading_pipeline = (
                f"videotestsrc pattern=black ! "
                f"video/x-raw,width=1920,height=1080,framerate=30/1 ! "
                f"textoverlay name=loading_text text=\"Loading\" "
                f"valignment=center halignment=center font-desc=\"Sans Bold 24\" ! "
                f"videoconvert ! video/x-raw,format=NV12 ! "
                f"kmssink plane-id={self.plane_id} connector-id={self.connector_id} sync=false"
            )

            logger.debug("[DRMAdBlocker] Creating loading pipeline...")
            self.pipeline = Gst.parse_launch(loading_pipeline)

            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect('message::error', self._on_error)

            # Get the textoverlay element for animation
            self._loading_textoverlay = self.pipeline.get_by_name('loading_text')

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("[DRMAdBlocker] Failed to start loading pipeline")
                return False

            self.is_visible = True
            self.current_source = 'loading'

            # Start the loading animation thread
            self._start_loading_animation()

            logger.info("[DRMAdBlocker] Loading display started")
            return True
        except Exception as e:
            logger.error(f"[DRMAdBlocker] Failed to start loading mode: {e}")
            return False

    def _start_loading_animation(self):
        """Start the loading dots animation thread."""
        self._stop_loading_anim = threading.Event()
        self._loading_anim_thread = threading.Thread(
            target=self._loading_animation_loop,
            daemon=True,
            name="LoadingAnimation"
        )
        self._loading_anim_thread.start()

    def _stop_loading_animation(self):
        """Stop the loading dots animation thread."""
        if hasattr(self, '_stop_loading_anim'):
            self._stop_loading_anim.set()
        if hasattr(self, '_loading_anim_thread') and self._loading_anim_thread:
            self._loading_anim_thread.join(timeout=1.0)
            self._loading_anim_thread = None
        self._loading_textoverlay = None

    def _loading_animation_loop(self):
        """Animate the loading text with ellipses (0-4 dots, increasing then decreasing)."""
        # Pattern: "", ".", "..", "...", "....", "...", "..", "."
        dot_counts = [0, 1, 2, 3, 4, 3, 2, 1]
        idx = 0
        interval = 0.3  # Update every 300ms

        while not self._stop_loading_anim.is_set():
            if hasattr(self, '_loading_textoverlay') and self._loading_textoverlay:
                dots = "." * dot_counts[idx]
                text = f"Loading{dots}"
                try:
                    self._loading_textoverlay.set_property('text', text)
                except Exception:
                    pass  # Pipeline may have been destroyed

            idx = (idx + 1) % len(dot_counts)
            self._stop_loading_anim.wait(interval)

    def _on_error(self, bus, message):
        err, debug = message.parse_error()
        self._pipeline_errors += 1
        self._last_error_time = time.time()
        logger.error(f"[DRMAdBlocker] Pipeline error: {err.message}")
        error_msg = err.message.lower() if err.message else ""
        if any(kw in error_msg for kw in ['connection', 'refused', 'timeout', 'socket', 'http']):
            if not self.is_visible:
                threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_eos(self, bus, message):
        logger.warning("[DRMAdBlocker] Unexpected EOS")
        if not self.is_visible:
            threading.Thread(target=self._restart_pipeline, daemon=True).start()

    def _on_warning(self, bus, message):
        warn, debug = message.parse_warning()
        logger.warning(f"[DRMAdBlocker] Pipeline warning: {warn.message}")

    def get_pipeline_health(self):
        if not self.pipeline:
            return {'healthy': False, 'state': 'stopped', 'errors': self._pipeline_errors}
        state_ret, state, pending = self.pipeline.get_state(0)
        return {
            'healthy': state == Gst.State.PLAYING,
            'state': state.value_nick if state else 'unknown',
            'errors': self._pipeline_errors,
            'last_error': self._last_error_time
        }

    def _get_blocking_text(self, source='default'):
        if source == 'hdmi_lost':
            return "NO SIGNAL\n\nHDMI disconnected\n\nWaiting for signal..."
        if source == 'no_hdmi_device':
            return "NO HDMI INPUT\n\nWaiting for HDMI signal..."
        if source == 'ocr':
            header = "BLOCKING (OCR)"
        elif source == 'vlm':
            header = "BLOCKING (VLM)"
        elif source == 'both':
            header = "BLOCKING (OCR+VLM)"
        else:
            header = "BLOCKING AD"
        vocab = random.choice(SPANISH_VOCABULARY)
        spanish, pronunciation, english, example = vocab
        return f"{header}\n\n{spanish}\n({pronunciation})\n= {english}\n\n{example}"

    def _get_debug_text(self):
        uptime_str = "N/A"
        if self.minus and hasattr(self.minus, 'start_time'):
            uptime_secs = int(time.time() - self.minus.start_time)
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"

        current_block_time = 0
        if self._current_block_start:
            current_block_time = time.time() - self._current_block_start

        total_block_secs = int(self._total_blocking_time + current_block_time)
        block_mins, block_secs = divmod(total_block_secs, 60)
        block_hours, block_mins = divmod(block_mins, 60)
        block_time_str = f"{block_hours}h {block_mins}m {block_secs}s" if block_hours > 0 else f"{block_mins}m {block_secs}s"

        # Format time saved
        time_saved_secs = int(self._total_time_saved)
        saved_mins, saved_secs = divmod(time_saved_secs, 60)
        saved_hours, saved_mins = divmod(saved_mins, 60)
        if saved_hours > 0:
            time_saved_str = f"{saved_hours}h {saved_mins}m {saved_secs}s"
        elif saved_mins > 0:
            time_saved_str = f"{saved_mins}m {saved_secs}s"
        else:
            time_saved_str = f"{saved_secs}s"

        debug_text = f"Uptime: {uptime_str}\nAds blocked: {self._total_ads_blocked}\nBlock time: {block_time_str}\nTime saved: {time_saved_str}"
        if self._skip_text:
            debug_text += f"\n{self._skip_text}"
        return debug_text

    def _rotation_loop(self, source):
        while not self._stop_rotation.is_set():
            text = self._get_blocking_text(source)
            self._blocking_api_call('/blocking/set', {'text_vocab': text})
            self._stop_rotation.wait(random.uniform(11.0, 15.0))

    def _start_rotation(self, source):
        self._stop_rotation.clear()
        self._rotation_thread = threading.Thread(target=self._rotation_loop, args=(source,), daemon=True)
        self._rotation_thread.start()

    def _stop_rotation_thread(self):
        self._stop_rotation.set()
        if self._rotation_thread:
            self._rotation_thread.join(timeout=1.0)
            self._rotation_thread = None

    def _debug_loop(self):
        while not self._stop_debug.is_set():
            if self._debug_overlay_enabled:
                self._blocking_api_call('/blocking/set', {'text_stats': self._get_debug_text()})
            self._stop_debug.wait(self._debug_interval)

    def _start_debug(self):
        if not self._debug_overlay_enabled:
            self._blocking_api_call('/blocking/set', {'text_stats': ''})
            return
        self._stop_debug.clear()
        self._debug_thread = threading.Thread(target=self._debug_loop, daemon=True, name="DebugUpdate")
        self._debug_thread.start()

    def _stop_debug_thread(self):
        self._stop_debug.set()
        if self._debug_thread:
            self._debug_thread.join(timeout=2.0)
            self._debug_thread = None

    def _start_snapshot_buffer(self):
        self._stop_snapshot_buffer.clear()
        self._snapshot_buffer_thread = threading.Thread(target=self._snapshot_buffer_loop, daemon=True, name="SnapshotBuffer")
        self._snapshot_buffer_thread.start()

    def _stop_snapshot_buffer_thread(self):
        self._stop_snapshot_buffer.set()
        if self._snapshot_buffer_thread:
            self._snapshot_buffer_thread.join(timeout=2.0)
            self._snapshot_buffer_thread = None

    def _snapshot_buffer_loop(self):
        while not self._stop_snapshot_buffer.is_set():
            try:
                url = f"http://localhost:{self.ustreamer_port}/snapshot"
                with urllib.request.urlopen(url, timeout=1.0) as response:
                    self._snapshot_buffer.append({'data': response.read(), 'time': time.time()})
            except Exception:
                pass
            self._stop_snapshot_buffer.wait(self._snapshot_interval)

    def _upload_background(self):
        if not self._snapshot_buffer:
            logger.warning("[DRMAdBlocker] No snapshots in buffer for background")
            return False

        logger.info(f"[DRMAdBlocker] Uploading background ({len(self._snapshot_buffer)} snapshots in buffer)")
        snapshot_data = self._snapshot_buffer[0]['data']
        original_size = len(snapshot_data)

        try:
            import cv2
            import numpy as np
            nparr = np.frombuffer(snapshot_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                factor = 20
                small = cv2.resize(img, (max(1, w // factor), max(1, h // factor)), interpolation=cv2.INTER_LINEAR)
                pixelated = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
                pixelated = (pixelated * 0.6).astype(np.uint8)
                _, encoded = cv2.imencode('.jpg', pixelated, [cv2.IMWRITE_JPEG_QUALITY, 80])
                snapshot_data = encoded.tobytes()
                logger.info(f"[DRMAdBlocker] Pixelated background: {w}x{h}, {len(snapshot_data)} bytes")
            else:
                logger.warning("[DRMAdBlocker] Failed to decode snapshot for pixelation")
        except ImportError:
            logger.warning("[DRMAdBlocker] OpenCV not available for pixelation")
        except Exception as e:
            logger.warning(f"[DRMAdBlocker] Pixelation failed: {e}")

        result = self._blocking_api_call('/blocking/background', data=snapshot_data, method='POST', timeout=2.0)
        success = result is not None and result.get('ok', False)
        if success:
            logger.info(f"[DRMAdBlocker] Background uploaded successfully")
        else:
            logger.warning(f"[DRMAdBlocker] Background upload failed: {result}")
        return success

    def _ease_out(self, t):
        return 1 - (1 - t) ** 2

    def _ease_in(self, t):
        return t ** 2

    def _stop_animation_thread(self):
        self._stop_animation.set()
        if self._animation_thread:
            self._animation_thread.join(timeout=2.0)
            self._animation_thread = None
        self._animating = False
        self._animation_direction = None

    def _start_animation(self, direction, source=None):
        self._stop_animation_thread()
        self._stop_animation.clear()
        self._animation_source = source
        self._animating = True
        self._animation_direction = direction
        self._animation_thread = threading.Thread(target=self._animation_loop, args=(direction,), daemon=True, name=f"Animation-{direction}")
        self._animation_thread.start()

    def _animation_loop(self, direction):
        start_time = time.time()
        duration = self._animation_duration_start if direction == 'start' else self._animation_duration_end

        full_x, full_y = 0, 0
        full_w, full_h = self._frame_width, self._frame_height
        corner_x = self._frame_width - self._preview_w - self._preview_padding
        corner_y = self._frame_height - self._preview_h - self._preview_padding
        corner_w, corner_h = self._preview_w, self._preview_h

        while not self._stop_animation.is_set():
            elapsed = time.time() - start_time
            progress = min(1.0, elapsed / duration)

            if direction == 'start':
                t = self._ease_out(progress)
                x = int(full_x + (corner_x - full_x) * t)
                y = int(full_y + (corner_y - full_y) * t)
                w = int(full_w + (corner_w - full_w) * t)
                h = int(full_h + (corner_h - full_h) * t)
            else:
                t = self._ease_in(progress)
                x = int(corner_x + (full_x - corner_x) * t)
                y = int(corner_y + (full_y - corner_y) * t)
                w = int(corner_w + (full_w - corner_w) * t)
                h = int(corner_h + (full_h - corner_h) * t)

            self._blocking_api_call('/blocking/set', {'preview_x': str(x), 'preview_y': str(y), 'preview_w': str(w), 'preview_h': str(h)})

            if progress >= 1.0:
                break
            time.sleep(0.016)

        # Set final position
        if direction == 'start':
            self._blocking_api_call('/blocking/set', {'preview_x': str(corner_x), 'preview_y': str(corner_y), 'preview_w': str(corner_w), 'preview_h': str(corner_h)})
        else:
            self._blocking_api_call('/blocking/set', {'preview_x': '0', 'preview_y': '0', 'preview_w': str(full_w), 'preview_h': str(full_h)})

        self._animating = False
        self._animation_direction = None
        if direction == 'start':
            self._on_start_animation_complete()
        else:
            self._on_end_animation_complete()

    def _on_start_animation_complete(self):
        logger.debug("[DRMAdBlocker] Start animation complete")
        source = self._animation_source or 'default'
        self._blocking_api_call('/blocking/set', {'text_vocab': self._get_blocking_text(source)})
        self._start_rotation(source)
        self._current_block_start = time.time()
        self._total_ads_blocked += 1
        self._start_debug()

    def _on_end_animation_complete(self):
        logger.debug("[DRMAdBlocker] End animation complete")
        self._blocking_api_call('/blocking/set', {'enabled': 'false'}, timeout=0.5)
        if self.audio:
            self.audio.unmute()

    def set_minus(self, minus_instance):
        self.minus = minus_instance

    def set_audio(self, audio):
        self.audio = audio

    def is_preview_enabled(self):
        return self._preview_enabled

    def set_preview_enabled(self, enabled):
        self._preview_enabled = enabled
        logger.info(f"[DRMAdBlocker] Preview {'enabled' if enabled else 'disabled'}")
        if self.is_visible:
            self._blocking_api_call('/blocking/set', {'preview_enabled': 'true' if enabled else 'false'})

    def is_debug_overlay_enabled(self):
        return self._debug_overlay_enabled

    def set_debug_overlay_enabled(self, enabled):
        self._debug_overlay_enabled = enabled
        logger.info(f"[DRMAdBlocker] Debug overlay {'enabled' if enabled else 'disabled'}")
        if self.is_visible:
            if enabled:
                if not self._debug_thread or not self._debug_thread.is_alive():
                    self._start_debug()
            else:
                self._stop_debug_thread()
                self._blocking_api_call('/blocking/set', {'text_stats': ''})

    def set_skip_status(self, available: bool, text: str = None):
        self._skip_available = available
        self._skip_text = text

    def get_skip_status(self) -> tuple:
        return (self._skip_available, self._skip_text)

    def add_time_saved(self, seconds: float):
        """Add to the total time saved by skipping ads."""
        self._total_time_saved += seconds
        logger.info(f"[DRMAdBlocker] Time saved: +{seconds:.0f}s (total: {self._total_time_saved:.0f}s)")

    def get_time_saved(self) -> float:
        """Get total time saved in seconds."""
        return self._total_time_saved

    def set_test_mode(self, duration_seconds: float):
        self._test_blocking_until = time.time() + duration_seconds
        logger.info(f"[DRMAdBlocker] Test mode enabled for {duration_seconds}s")

    def clear_test_mode(self):
        self._test_blocking_until = 0
        logger.info("[DRMAdBlocker] Test mode cleared")

    def is_test_mode_active(self) -> bool:
        return self._test_blocking_until > time.time()

    def show(self, source='default'):
        with self._lock:
            if not self.pipeline:
                logger.warning("[DRMAdBlocker] Pipeline not initialized")
                return

            if self.is_visible and self._animation_direction != 'end':
                if self.current_source != source:
                    self.current_source = source
                return

            if self._animating and self._animation_direction == 'start':
                if self.current_source != source:
                    self.current_source = source
                return

            if self._animating and self._animation_direction == 'end':
                logger.info(f"[DRMAdBlocker] Reversing end animation ({source})")
                self._stop_animation_thread()

            logger.info(f"[DRMAdBlocker] Starting blocking ({source})")

            self._upload_background()

            if self.audio:
                self.audio.mute()

            self._blocking_api_call('/blocking/set', {
                'enabled': 'true',
                'preview_x': '0', 'preview_y': '0',
                'preview_w': str(self._frame_width), 'preview_h': str(self._frame_height),
                'preview_enabled': 'true' if self._preview_enabled else 'false',
                'text_vocab': '', 'text_stats': ''
            }, timeout=0.5)

            self.is_visible = True
            self.current_source = source

            if self.minus:
                self.minus.blocking_active = True

            self._start_animation('start', source)

    def hide(self, force=False):
        if not force and self._test_blocking_until > time.time():
            return

        with self._lock:
            if self._animating and self._animation_direction == 'end':
                return

            was_visible = self.is_visible
            self.is_visible = False
            self.current_source = None

            if self.minus:
                self.minus.blocking_active = False

            self._stop_rotation_thread()
            self._stop_debug_thread()

            if self._current_block_start:
                self._total_blocking_time += time.time() - self._current_block_start
                self._current_block_start = None

            self._blocking_api_call('/blocking/set', {'text_vocab': '', 'text_stats': ''})

            if not self.pipeline:
                if was_visible:
                    logger.warning("[DRMAdBlocker] Pipeline not initialized")
                if self.audio:
                    self.audio.unmute()
                return

            if not was_visible and self._animation_direction != 'start':
                return

            if self._animating:
                self._stop_animation_thread()

            logger.info("[DRMAdBlocker] Starting end animation")
            self._start_animation('end', None)

    def update(self, ad_detected, is_skippable=False, skip_location=None, ocr_detected=False, vlm_detected=False):
        if ad_detected and not is_skippable:
            if ocr_detected and vlm_detected:
                source = 'both'
            elif ocr_detected:
                source = 'ocr'
            elif vlm_detected:
                source = 'vlm'
            else:
                source = 'default'
            self.show(source)
        else:
            self.hide()

    def destroy(self):
        with self._lock:
            self._stop_watchdog_thread()
            self._stop_rotation_thread()
            self._stop_debug_thread()
            self._stop_animation_thread()
            self._stop_snapshot_buffer_thread()
            self._stop_loading_animation()

            self._blocking_api_call('/blocking/set', {'clear': 'true'}, timeout=0.5)

            if self.pipeline:
                try:
                    if self.bus:
                        self.bus.remove_signal_watch()
                        self.bus = None
                    self.pipeline.set_state(Gst.State.NULL)
                    logger.info("[DRMAdBlocker] Pipeline stopped")
                except Exception as e:
                    logger.error(f"[DRMAdBlocker] Error stopping pipeline: {e}")
                self.pipeline = None

            self.is_visible = False


AdBlocker = DRMAdBlocker
