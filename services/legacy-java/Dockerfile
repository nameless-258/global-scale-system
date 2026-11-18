# Compilação (multi-stage build)
FROM eclipse-temurin:21-jdk AS build
WORKDIR /build
COPY src/BancoParceiroMainframe.java .
RUN javac -d out BancoParceiroMainframe.java

# Execução
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /build/out /app
EXPOSE 8443
CMD ["java", "-cp", "/app", "BancoParceiroMainframe"]
