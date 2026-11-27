import java.io.File

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

val releaseSigningVariableNames = listOf(
    "NESTOCK_ANDROID_KEYSTORE_PATH",
    "NESTOCK_ANDROID_STORE_PASSWORD",
    "NESTOCK_ANDROID_KEY_ALIAS",
    "NESTOCK_ANDROID_KEY_PASSWORD",
)
val expectedReleaseCertificateSha256 =
    "7e940a27cde13001c0cffd0b676c3723ea8cc2622f5ecb35a729bad6af396d27"
val releaseSigningValues = releaseSigningVariableNames.associateWith { System.getenv(it) }
val releaseBuildRequested = gradle.startParameter.taskNames.any { taskName ->
    taskName.substringAfterLast(':').contains("release", ignoreCase = true)
}

if (releaseBuildRequested) {
    val missingVariables = releaseSigningValues
        .filterValues { it.isNullOrBlank() }
        .keys
    if (missingVariables.isNotEmpty()) {
        throw GradleException(
            "Release signing is unavailable. Set these local environment variables: " +
                missingVariables.joinToString(", "),
        )
    }

    val keystorePath = releaseSigningValues.getValue("NESTOCK_ANDROID_KEYSTORE_PATH")!!
    val keystoreFile = File(keystorePath)
    if (!keystoreFile.isAbsolute || !keystoreFile.isFile) {
        throw GradleException(
            "Release signing keystore path must be absolute and identify an existing file.",
        )
    }

    val keytoolName = if (System.getProperty("os.name").startsWith("Windows", ignoreCase = true)) {
        "keytool.exe"
    } else {
        "keytool"
    }
    val keytool = File(System.getProperty("java.home"), "bin/$keytoolName")
    if (!keytool.isFile) {
        throw GradleException("Release signing requires keytool from the active JDK.")
    }
    val keyAlias = releaseSigningValues.getValue("NESTOCK_ANDROID_KEY_ALIAS")!!
    val keytoolProcess = try {
        ProcessBuilder(
            keytool.absolutePath,
            "-list",
            "-v",
            "-keystore",
            keystorePath,
            "-alias",
            keyAlias,
            "-storepass:env",
            "NESTOCK_ANDROID_STORE_PASSWORD",
        )
            .redirectErrorStream(true)
            .start()
    } catch (_: Exception) {
        throw GradleException("Release signing could not inspect the configured certificate.")
    }
    val keytoolOutput = keytoolProcess.inputStream.bufferedReader().use { it.readText() }
    if (keytoolProcess.waitFor() != 0) {
        throw GradleException("Release signing could not inspect the configured certificate.")
    }
    val actualFingerprint = Regex(
        """SHA256:\s*((?:[0-9A-Fa-f]{2}:?){32})""",
    )
        .find(keytoolOutput)
        ?.groupValues
        ?.get(1)
        ?.replace(":", "")
        ?.lowercase()
    if (actualFingerprint != expectedReleaseCertificateSha256) {
        throw GradleException("Release signing certificate does not match the approved update signer.")
    }
}

android {
    namespace = "com.example.nestock"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.example.nestock"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        create("release") {
            releaseSigningValues["NESTOCK_ANDROID_KEYSTORE_PATH"]
                ?.takeIf { it.isNotBlank() }
                ?.let { storeFile = file(it) }
            storePassword = releaseSigningValues["NESTOCK_ANDROID_STORE_PASSWORD"]
            keyAlias = releaseSigningValues["NESTOCK_ANDROID_KEY_ALIAS"]
            keyPassword = releaseSigningValues["NESTOCK_ANDROID_KEY_PASSWORD"]
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
