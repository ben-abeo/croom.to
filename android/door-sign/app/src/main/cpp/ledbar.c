/*
 * The SMT1019 panel's light bar. Its controller is /dev/ledjni: the panel's own LED test (libjnielc, ledseek) opens it read-only
 * and sends one ioctl per channel, 161 red, 162 green and 163 blue, with the level, 0 to 15, as the argument itself.
 */
#include <fcntl.h>
#include <jni.h>
#include <sys/ioctl.h>
#include <unistd.h>

JNIEXPORT jint JNICALL Java_to_croom_doorsign_LedDevice_nativeOpen(JNIEnv *env, jclass cls) {
    return open("/dev/ledjni", O_RDONLY | O_NOCTTY | O_CLOEXEC);
}

JNIEXPORT jint JNICALL Java_to_croom_doorsign_LedDevice_nativeSet(JNIEnv *env, jclass cls, jint fd, jint channel, jint level) {
    return ioctl(fd, channel, (unsigned long) level);
}

JNIEXPORT void JNICALL Java_to_croom_doorsign_LedDevice_nativeClose(JNIEnv *env, jclass cls, jint fd) {
    close(fd);
}
