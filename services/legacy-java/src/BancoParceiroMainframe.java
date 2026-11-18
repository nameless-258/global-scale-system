import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.X509EncodedKeySpec;
import java.time.Instant;
import java.util.Base64;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Pattern;

import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

/**
 * Simulação do "mainframe" legado de um banco parceiro.
 *
 * Tecnologia DIFERENTE do núcleo (Java vs Python) e formato de dados DIFERENTE
 * (XML vs JSON) — demonstra a INTEROPERABILIDADE de sistemas heterogéneos.
 *
 * Segurança:
 *  - HTTPS (TLS) com certificado emitido pela CA da Global Scale;
 *  - verificação do digest SHA-256 do corpo (integridade);
 *  - verificação da assinatura digital SHA256withRSA com a chave pública do núcleo
 *    (autenticidade + não-repúdio);
 *  - parser XML endurecido (sem DTD/entidades externas -> imune a XXE).
 *
 * Concorrência: pedidos servidos por um pool de threads; registo de ordens num
 * ConcurrentHashMap (thread-safe); idempotência pela referência da ordem.
 */
public class BancoParceiroMainframe {

    // Limite por ordem em cada moeda aceite pelo banco parceiro
    private static final Map<String, BigDecimal> LIMITE_POR_ORDEM = Map.of(
            "MZN", new BigDecimal("1000000.00"),
            "EUR", new BigDecimal("15000.00"),
            "USD", new BigDecimal("15000.00"));
    private static final Pattern IBAN = Pattern.compile("^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$");
    private static final Map<String, Map<String, String>> ORDENS = new ConcurrentHashMap<>();
    private static final AtomicLong SEQ = new AtomicLong(700000);
    private static PublicKey chaveNucleo;

    public static void main(String[] args) throws Exception {
        int porta = Integer.parseInt(env("PORTA", "8443"));
        String keystore = env("KEYSTORE", "/certs/legacy.p12");
        char[] pass = env("KEYSTORE_PASS", "globalscale").toCharArray();
        chaveNucleo = carregarChavePublica(env("CHAVE_PUBLICA_NUCLEO", "/certs/core-signing-public.pem"));

        KeyStore ks = KeyStore.getInstance("PKCS12");
        try (FileInputStream in = new FileInputStream(keystore)) { ks.load(in, pass); }
        KeyManagerFactory kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        kmf.init(ks, pass);
        SSLContext ssl = SSLContext.getInstance("TLS");
        ssl.init(kmf.getKeyManagers(), null, null);

        HttpsServer srv = HttpsServer.create(new InetSocketAddress(porta), 0);
        srv.setHttpsConfigurator(new HttpsConfigurator(ssl));
        srv.setExecutor(Executors.newFixedThreadPool(8));
        srv.createContext("/mainframe/saude", BancoParceiroMainframe::saude);
        srv.createContext("/mainframe/ordens", BancoParceiroMainframe::ordens);
        srv.start();
        log("Mainframe do Banco Parceiro (Java " + System.getProperty("java.version")
                + ") a escutar em HTTPS :" + porta);
    }

    // ------------------------------------------------------------------ Rotas
    private static void saude(HttpExchange ex) throws IOException {
        Map<String, String> r = new LinkedHashMap<>();
        r.put("Sistema", "MAINFRAME-BANCO-PARCEIRO");
        r.put("Tecnologia", "Java " + System.getProperty("java.version"));
        r.put("Estado", "OK");
        r.put("OrdensRegistadas", String.valueOf(ORDENS.size()));
        responder(ex, 200, xml("Saude", r));
    }

    private static void ordens(HttpExchange ex) throws IOException {
        try {
            String caminho = ex.getRequestURI().getPath();
            String metodo = ex.getRequestMethod();
            if ("GET".equals(metodo) && caminho.startsWith("/mainframe/ordens/")) {
                String ref = caminho.substring("/mainframe/ordens/".length());
                Map<String, String> o = ORDENS.get(ref);
                if (o == null) { responder(ex, 404, erro(ref, "Ordem inexistente")); return; }
                responder(ex, 200, xml("RespostaOrdem", o));
            } else if ("GET".equals(metodo)) {
                StringBuilder sb = new StringBuilder("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Ordens>");
                ORDENS.values().forEach(o -> sb.append(xml("RespostaOrdem", o).replaceFirst("<\\?xml[^>]*\\?>", "")));
                responder(ex, 200, sb.append("</Ordens>").toString());
            } else if ("POST".equals(metodo)) {
                receberOrdem(ex);
            } else {
                responder(ex, 405, erro("-", "Método não suportado"));
            }
        } catch (org.xml.sax.SAXException e) {
            log("REJEITADO: XML inválido ou inseguro (" + e.getMessage() + ")");
            responder(ex, 400, erro("-", "XML inválido ou inseguro (DTD/entidades externas proibidas)"));
        } catch (Exception e) {
            log("ERRO: " + e);
            responder(ex, 500, erro("-", "Erro interno: " + e.getMessage()));
        }
    }

    private static void receberOrdem(HttpExchange ex) throws Exception {
        byte[] corpo = ex.getRequestBody().readAllBytes();

        // 1) Integridade: digest SHA-256 do corpo
        String digestRecebido = ex.getRequestHeaders().getFirst("X-Digest-SHA256");
        String digestCalculado = sha256(corpo);
        if (digestRecebido == null || !MessageDigest.isEqual(
                digestRecebido.getBytes(StandardCharsets.US_ASCII),
                digestCalculado.getBytes(StandardCharsets.US_ASCII))) {
            log("REJEITADO: digest SHA-256 não confere (mensagem adulterada)");
            responder(ex, 400, erro("-", "Integridade violada: SHA-256 não confere"));
            return;
        }
        // 2) Autenticidade: assinatura digital RSA do núcleo
        String assinatura = ex.getRequestHeaders().getFirst("X-Assinatura");
        if (assinatura == null || !verificarAssinatura(corpo, assinatura)) {
            log("REJEITADO: assinatura digital inválida");
            responder(ex, 401, erro("-", "Assinatura digital inválida"));
            return;
        }
        // 3) Parsing seguro do XML
        Map<String, String> o = lerXml(corpo);
        String ref = o.getOrDefault("Referencia", "");
        log("Ordem recebida de " + ex.getRequestHeaders().getFirst("X-Origem")
                + " (assinatura OK, SHA-256 OK): " + o);

        // 4) Idempotência: a mesma referência devolve sempre a mesma resposta
        Map<String, String> existente = ORDENS.get(ref);
        if (existente != null) { responder(ex, 200, xml("RespostaOrdem", existente)); return; }

        // 5) Regras de negócio do banco parceiro
        Map<String, String> r = new LinkedHashMap<>();
        r.put("Referencia", ref);
        BigDecimal valor = new BigDecimal(o.getOrDefault("Valor", "0"));
        String iban = o.getOrDefault("IbanDestino", "").replace(" ", "").toUpperCase();
        String moeda = o.getOrDefault("Moeda", "MZN").toUpperCase();
        BigDecimal limite = LIMITE_POR_ORDEM.get(moeda);
        if (!IBAN.matcher(iban).matches()) {
            r.put("Estado", "REJEITADA"); r.put("Motivo", "IBAN de destino inválido");
        } else if (limite == null) {
            r.put("Estado", "REJEITADA"); r.put("Motivo", "Moeda não suportada: " + moeda);
        } else if (valor.compareTo(limite) > 0) {
            r.put("Estado", "REJEITADA");
            r.put("Motivo", "Valor excede o limite por ordem (" + limite.toPlainString() + " " + moeda + ")");
        } else {
            r.put("Estado", "ACEITE");
            r.put("CodigoAutorizacao", "MF" + SEQ.incrementAndGet());
            r.put("Motivo", "");
        }
        r.put("Valor", valor.toPlainString());
        r.put("Moeda", moeda);
        r.put("ProcessadoEm", Instant.now().toString());
        r.put("Sistema", "MAINFRAME-JAVA");
        ORDENS.put(ref, r);
        log("Registo de largura fixa (estilo COBOL): " + registoLarguraFixa(ref, iban, valor, moeda, r.get("Estado")));
        responder(ex, 200, xml("RespostaOrdem", r));
    }

    // ------------------------------------------------------------- Utilitários
    private static Map<String, String> lerXml(byte[] corpo) throws Exception {
        DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();
        f.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);   // anti-XXE
        f.setFeature("http://xml.org/sax/features/external-general-entities", false);
        f.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        f.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        f.setExpandEntityReferences(false);
        f.setNamespaceAware(true);
        DocumentBuilder b = f.newDocumentBuilder();
        Document d = b.parse(new ByteArrayInputStream(corpo));
        Map<String, String> m = new LinkedHashMap<>();
        NodeList filhos = d.getDocumentElement().getChildNodes();
        for (int i = 0; i < filhos.getLength(); i++) {
            Node n = filhos.item(i);
            if (n instanceof Element el) m.put(el.getLocalName(), el.getTextContent().trim());
        }
        return m;
    }

    private static String xml(String raiz, Map<String, String> campos) {
        StringBuilder sb = new StringBuilder("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
        sb.append('<').append(raiz).append(" xmlns=\"urn:globalscale:interbancario\">");
        campos.forEach((k, v) -> sb.append('<').append(k).append('>').append(escapar(v))
                .append("</").append(k).append('>'));
        return sb.append("</").append(raiz).append('>').toString();
    }

    private static String erro(String ref, String motivo) {
        Map<String, String> r = new LinkedHashMap<>();
        r.put("Referencia", ref); r.put("Estado", "REJEITADA"); r.put("Motivo", motivo);
        return xml("RespostaOrdem", r);
    }

    private static String escapar(String s) {
        return s == null ? "" : s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    private static void responder(HttpExchange ex, int status, String xml) throws IOException {
        byte[] b = xml.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/xml; charset=utf-8");
        ex.getResponseHeaders().set("X-Digest-SHA256", sha256(b));
        ex.getResponseHeaders().set("X-Sistema", "MAINFRAME-JAVA");
        ex.sendResponseHeaders(status, b.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(b); }
    }

    private static boolean verificarAssinatura(byte[] dados, String b64) {
        try {
            Signature s = Signature.getInstance("SHA256withRSA");
            s.initVerify(chaveNucleo);
            s.update(dados);
            return s.verify(Base64.getDecoder().decode(b64));
        } catch (Exception e) {
            return false;
        }
    }

    private static PublicKey carregarChavePublica(String caminho) throws Exception {
        String pem = Files.readString(Path.of(caminho))
                .replaceAll("-----(BEGIN|END) PUBLIC KEY-----", "").replaceAll("\\s", "");
        return KeyFactory.getInstance("RSA")
                .generatePublic(new X509EncodedKeySpec(Base64.getDecoder().decode(pem)));
    }

    private static String sha256(byte[] b) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(b));
        } catch (Exception e) { throw new IllegalStateException(e); }
    }

    private static String registoLarguraFixa(String ref, String iban, BigDecimal v, String moeda,
                                             String estado) {
        return String.format("%-36s|%-34s|%015d|%-3s|%-9s", ref, iban,
                v.movePointRight(2).longValue(), moeda, estado);
    }

    private static String env(String k, String def) {
        String v = System.getenv(k);
        return v == null || v.isBlank() ? def : v;
    }

    private static void log(String m) {
        System.out.println("[" + Instant.now() + "] [MAINFRAME] " + m);
    }
}
