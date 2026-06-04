"""
rd.py - Tiny container runner for Rubber Docker: Mount Namespace


Main ideas:
    - Create a new root filesystem from an Ubuntu tar image.
    - Start a child process using linux xlone().
    - Put that child process in a new mount namespace using CLONE_NEWNS.
    - Mount container-specific filesystems like /proc, /sys, /dev, and /dev/pts.
    - Create basic device files like /dev/null and /dev/urandom.
    - Use chroot() so the child process sees the extracted Ubuntu filesystem as /.
    - Use execv() to replace the child Python process with the requested command.

Note:
    - This is not afully secured container yet
    - chroot() changes the process's view of the filesystem, but it is not complete isolation
    - Later levels improve this using pivot_root, PID namespaces, user namespaces, etc.
"""

import click
import os
import sys
import tarfile
import tempfile
import traceback
import uuid
import stat

# Import linux module is not a normal Python file in this folder.
# It is acompiled extension module located in the project root:
#   rubber-docker/linux.cpython-314-x86_64-linux-gnu.so
# This module gives Python access to Linux system calls and constants such as:
# linux.clone(). linux.mount(), linux.CLONE_NEWS, linux.MS_PRIVATE. linux.MS_REC
# Project root was added to sys.path so Python imports teh correct Rubber Docker linux module
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, PROJECT_ROOT)

import linux

# run using: sudo /home/ivan/Desktop/Projects/rubber-docker/.venv/bin/python rd.py run -i ubuntu /bin/bash

def create_container_root(image_name, image_dir, container_id, container_dir):
    """
    Create a new root filesystem for the container.

    A container needs its own filesystem tree. In this project, the filesystem comes from a tar file,
    for example:
    /workshop/images/ubuntu.tar

    This function extracts that image into a unique container directory:
    /workshop/containers/<container_id>rootfs

    After extraction rootfs directory will later become the container's "/" using chroot().
    """

    # Build path to the image tar file.
    # image_dir = "/workshop/images"
    # image_name = "ubuntu"
    # image_path = /workshop/images/ubuntu.tar"
    image_path = os.path.join(image_dir, image_name + '.tar')

    # Build a unique container folder path
    # /workshop/containers/ something uuid made
    container_path = os.path.join(container_dir, container_id)

    # The actual root filesystem goes inside the container folder.
    # /workshop/containers/<id>/rootfs
    container_root = os.path.join(container_path, 'rootfs')
    
    # Create the rootfs directory
    os.makedirs(container_root)
    
    # Open the ubuntu imafe tar file.
    with tarfile.open(image_path) as t:
        # Fun fact: tar files may contain *nix devices! *facepalm*
        # Tar files can contain special Unix device files. Ex. (character devices, block devices)
        # Skipped because extracting device file directly from an image can be unsafe or awkward.
        # Instead, later in contain(), we create only the basic device we need, /dev/null and /dev/urandom
        members = [m for m in t.getmembers()
                   if m.type not in (tarfile.CHRTYPE, tarfile.BLKTYPE)]

        # Python 3.14 blocks some rootfs symlinks by default,
        # so we trust this image tar because it is our container image.
        t.extractall(container_root, members=members, filter='fully_trusted')
    
    # Return the path to the extracted root filesystem.
    return container_root


@click.group()
def cli():
    """
    Main command group

    This allows us to define subcommands such as:
        
        rd.py run -i ubuntu /bin/bash
    """
    pass


def contain(command, image_name, image_dir, container_id, container_dir):
    """
    Set up and run the container

    This function runs inside the child process created by linux.clone()

    This function:
        - Create the root filesystem.
        - set up private mounts.
        - Mount /proc, /sys, /dev, and /dev/pts.
        - Create basic device files
        - chroot into new root filesystem
        - execute the request command
    """

    # Create a fresh root filesystem for thsi container by extracting image.
    new_root = create_container_root(
        image_name, image_dir, container_id, container_dir)

    print('Created a new root fs for our container: {}'.format(new_root))

    # Make  mount propagation private
    # Mount propagation controls whether mount changes in one namespace spread into another namespace
    # MS_PRIVATE means:
    #   Mount changes here should not propagate somewhere else
    # MS_REC means:
    #   Apply this recursively to ecerything under /.
    # Together:
    #   Make / and all mounts under / private

    # This helps prevent the container's mountfrom leaking into the host's mount table
    linux.mount(None, '/', None, linux.MS_REC | linux.MS_PRIVATE, None)

    # Mount /proc inside the container root

    # /proc is a virtual filesystem that exposes process and kernel information.
    # Many Linux programs expect /proc to exist. For example:
    #     cat /proc/self/mountinfo
    # We mount it at:
    #     new_root/proc
    # After chroot(), the process sees that as:
    #     /proc

    linux.mount('proc', os.path.join(new_root, 'proc'), 'proc', 0, '')

    # Mount /sys inside the container root
    
    # /sys is another virtual filesystem. It exposes kernel and device
    # information.
    # We mount it at:
    #     new_root/sys
    # After chroot(), it appears as:
    #     /sys
    linux.mount('sysfs', os.path.join(new_root, 'sys'), 'sysfs', 0, '')

    # Mount /dev inside the container root

    # /dev contains device files.
    # Instead of using the host's /dev directly, we create a fresh tmpfs for
    # the container's /dev.
    # tmpfs means:
    #     a temporary in-memory filesystem.
    # MS_NOSUID means:
    #     ignore set-user-ID bits on this mount, which is safer.
    # mode=755 sets the permissions for the /dev directory.
    linux.mount('tmpfs', os.path.join(new_root, 'dev'), 'tmpfs',
                linux.MS_NOSUID | linux.MS_STRICTATIME, 'mode=755')

    # Mount /dev/pts
   
    # /dev/pts is used for pseudo-terminals.
    # When you open a terminal, Linux uses pseudo-terminal devices like:
    #     /dev/pts/0
    # Mounting devpts helps terminal-related programs work correctly inside
    # the container.
    devpts_path = os.path.join(new_root, 'dev', 'pts')

    if not os.path.exists(devpts_path):
        os.makedirs(devpts_path)

    linux.mount('devpts', devpts_path, 'devpts', 0, '')
    devpts_path = os.path.join(new_root, 'dev', 'pts')
    if not os.path.exists(devpts_path):
        os.makedirs(devpts_path)

    linux.mount('devpts', devpts_path, 'devpts', 0, '')

    # Create /dev/stdin, /dev/stdout, and /dev/stderr
    
    # Linux programs commonly expect these files:
    #     /dev/stdin
    #     /dev/stdout
    #     /dev/stderr
    # They are symbolic links to the current process's file descriptors:
    #     /proc/self/fd/0  -> standard input
    #     /proc/self/fd/1  -> standard output
    #     /proc/self/fd/2  -> standard error
    # This lets programs inside the container use normal input/output.
    for i, dev in enumerate(['stdin', 'stdout', 'stderr']):
        dev_path = os.path.join(new_root, 'dev', dev)

        if not os.path.exists(dev_path):
            os.symlink('/proc/self/fd/%d' % i, dev_path)

    # Create basic device files
 
    # The image extraction skipped device files, so we manually create a few
    # safe and useful ones.
    # os.mknod() creates filesystem nodes
    # stat.S_IFCHR means:
    #     create a character device.
    # os.makedev(major, minor) creates a device number.
    # Common Linux device numbers:
    #     /dev/null     = major 1, minor 3
    #     /dev/zero     = major 1, minor 5
    #     /dev/random   = major 1, minor 8
    #     /dev/urandom  = major 1, minor 9
    # 0o666 means:
    #     readable and writable by everyone.

    # /dev/null discards anything written to it.
    os.mknod(os.path.join(new_root, 'dev', 'null'),
             0o666 | stat.S_IFCHR, os.makedev(1, 3))
    
    # /dev/zero produces endless zero bytes when read.
    os.mknod(os.path.join(new_root, 'dev', 'zero'),
             0o666 | stat.S_IFCHR, os.makedev(1, 5))
    
    # /dev/random produces random bytes
    os.mknod(os.path.join(new_root, 'dev', 'random'),
             0o666 | stat.S_IFCHR, os.makedev(1, 8))
    
    # /dev/urandom produces random bytes
    os.mknod(os.path.join(new_root, 'dev', 'urandom'),
             0o666 | stat.S_IFCHR, os.makedev(1, 9))

    # Change the process root using chroot()
    
    # Before chroot():
    #     / means the host's root filesystem.
    # After chroot(new_root):
    #     / means the container root filesystem.
    # Example:
    #     new_root = /workshop/containers/<id>/rootfs
    # After chroot, when the process runs:
    #     ls /
    # it sees:
    #     /bin /etc /usr /proc /sys /dev ...
    # from the extracted Ubuntu image.
    # Note:
    #     chroot is not complete security by itself. Later levels improve this
    #     using pivot_root and more namespaces.
    os.chroot(new_root)

    # Move into the new root directory.
    # This is important because after chroot(), the process should not keep its
    # current working directory outside the new root.
    os.chdir('/')

    # Execute the requested command
    
    # execvp() replaces the current child Python process with another program.
    # If the user ran:
    #     rd.py run -i ubuntu /bin/bash
    # then:
    #     command[0] = "/bin/bash"
    #     command    = ("/bin/bash",)
    # After this line, the child process is no longer running Python.
    # It becomes /bin/bash inside the container.
    os.execvp(command[0], command)


@cli.command(context_settings=dict(ignore_unknown_options=True,))
@click.option('--image-name', '-i', help='Image name', default='ubuntu')
@click.option('--image-dir', help='Images directory',
              default='/workshop/images')
@click.option('--container-dir', help='Containers directory',
              default='/workshop/containers')
@click.argument('Command', required=True, nargs=-1)
def run(image_name, image_dir, container_dir, command):
    """
    Run a command inside a minimal container.

    Example:

        sudo python rd.py run -i ubuntu /bin/bash

    The command-line options mean:

        -i ubuntu
            Use /workshop/images/ubuntu.tar as the image.

        /bin/bash
            Run /bin/bash inside the container.

    This function creates a child process with a new mount namespace and then
    waits for that child process to exit.
    """

    # Create a unique container ID so each run gets its own rootfs directory.
    container_id = str(uuid.uuid4())

    # Create the container process
   
    # linux.clone() is similar to fork(), but it allows us to request specific
    # Linux namespace isolation.
    # Here we pass:
    #     contain
    #         The function the child process should run.
    #     linux.CLONE_NEWNS
    #         Create a new mount namespace for the child.
    #     (command, image_name, image_dir, container_id, container_dir)
    #         Arguments passed into contain().
    # A mount namespace controls what mounts a process can see.
    # Because the child has its own mount namespace, the container can mount
    # /proc, /sys, and /dev without directly changing the host's mount view.
    pid = linux.clone(
        contain,
        linux.CLONE_NEWNS,
        (command, image_name, image_dir, container_id, container_dir)
    )

    # Parent waits for the container process
    
    # The parent process waits until the child process exits.
    # If the child process runs /bin/bash, this line waits until you type:
    #     exit
    # inside the container shell.
    _, status = os.waitpid(pid, 0)
    print('{} exited with status {}'.format(pid, status))


if __name__ == '__main__':
    cli()
